"""Movie Recommendation System API (FastAPI + TMDB + TF-IDF)."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import asyncio
import json
import logging
from pathlib import Path
import random
import re
import time
from typing import Any, Optional
import os
import pickle

import httpx
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
load_dotenv(BASE_DIR / ".env")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"
TMDB_MAX_ATTEMPTS = 3
TMDB_BACKOFF_BASE = 0.25
TMDB_MAX_RETRY_AFTER = 30.0
TMDB_MAX_CONCURRENT_REQUESTS = 4
TMDB_CACHE: dict[str, tuple[float, float, dict]] = {}
_tmdb_client: Optional[httpx.AsyncClient] = None
_tmdb_semaphore: Optional[asyncio.Semaphore] = None
_tmdb_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None

# Local recommendation resources are loaded at startup. If an artifact is missing,
# the API still starts and reports the issue only for endpoints that need TF-IDF.
df: Optional[pd.DataFrame] = None
tfidf_matrix: Any = None
tfidf_obj: Any = None
indices_obj: Any = None
title_to_idx: dict[str, int] = {}
tfidf_load_error: Optional[str] = None


class MovieCard(BaseModel):
    tmdb_id: int
    title: str
    poster_url: Optional[str] = None
    release_date: Optional[str] = None
    vote_average: Optional[float] = None


class MovieDetails(MovieCard):
    overview: Optional[str] = None
    backdrop_url: Optional[str] = None
    genres: list[dict] = []


class TFIDFRecommendation(BaseModel):
    title: str
    score: float
    tmdb: Optional[MovieCard] = None


class RecommendationBundle(BaseModel):
    query: str
    movie_details: MovieDetails
    tfidf_recommendations: list[TFIDFRecommendation]
    genre_recommendations: list[MovieCard]


def norm_title(value: Any) -> str:
    return str(value).strip().lower()


def image_url(path: Optional[str]) -> Optional[str]:
    return f"{TMDB_IMG}{path}" if path else None


def load_recommendation_artifacts() -> None:
    global df, tfidf_matrix, tfidf_obj, indices_obj, title_to_idx, tfidf_load_error
    try:
        with (BASE_DIR / "df.pkl").open("rb") as file:
            df = pickle.load(file)
        with (BASE_DIR / "indices.pkl").open("rb") as file:
            indices_obj = pickle.load(file)
        with (BASE_DIR / "tfidf_matrix.pkl").open("rb") as file:
            tfidf_matrix = pickle.load(file)
        with (BASE_DIR / "tfidf.pkl").open("rb") as file:
            tfidf_obj = pickle.load(file)
        if not isinstance(df, pd.DataFrame) or "title" not in df.columns:
            raise ValueError("df.pkl must contain a pandas DataFrame with a 'title' column")
        pairs = indices_obj.items() if hasattr(indices_obj, "items") else None
        if pairs is None:
            raise ValueError("indices.pkl must be a mapping or pandas Series")
        title_to_idx = {norm_title(k): int(v) for k, v in pairs}
        tfidf_load_error = None
        print(f"Loaded TF-IDF artifacts: {len(df):,} movies")
    except Exception as exc:
        tfidf_load_error = f"Could not load recommendation artifacts: {exc}"
        print(tfidf_load_error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tmdb_client, _tmdb_semaphore, _tmdb_semaphore_loop
    load_recommendation_artifacts()
    timeout = httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0)
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    _tmdb_client = httpx.AsyncClient(timeout=timeout, limits=limits)
    _tmdb_semaphore = asyncio.Semaphore(TMDB_MAX_CONCURRENT_REQUESTS)
    _tmdb_semaphore_loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        await _tmdb_client.aclose()
        _tmdb_client = None
        _tmdb_semaphore = None
        _tmdb_semaphore_loop = None


app = FastAPI(title="Movie Recommendation System API", version="4.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


def require_tmdb_key() -> None:
    if not TMDB_API_KEY:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured. Add it to the .env file and restart the server.")


def _cache_policy(path: str) -> tuple[float, float]:
    if path.startswith("/search/"):
        return 60.0, 600.0
    if re.fullmatch(r"/movie/\d+", path):
        return 900.0, 86400.0
    if path.startswith("/trending/") or path.startswith("/movie/") or path.startswith("/discover/"):
        return 300.0, 3600.0
    return 0.0, 0.0


def _cache_key(path: str, params: dict) -> str:
    return f"{path}?{json.dumps(params, sort_keys=True, separators=(',', ':'))}"


def _safe_diagnostic_text(value: str) -> str:
    if TMDB_API_KEY:
        value = value.replace(TMDB_API_KEY, "[REDACTED]")
    value = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token)(\s*[:=]\s*|%3[dD])[^&\s,\"']+",
        r"\1=[REDACTED]",
        value,
    )
    return value[:500]


def _log_tmdb_failure(path: str, status: Optional[int], body: str) -> None:
    logger.warning(
        "TMDB request failed path=%s status=%s body=%s",
        path,
        status if status is not None else "connection_error",
        _safe_diagnostic_text(body),
    )


def _retry_after(response: httpx.Response) -> Optional[float]:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(seconds, 0.0), TMDB_MAX_RETRY_AFTER)


def _request_semaphore() -> asyncio.Semaphore:
    global _tmdb_semaphore, _tmdb_semaphore_loop
    loop = asyncio.get_running_loop()
    if _tmdb_semaphore is None or _tmdb_semaphore_loop is not loop:
        _tmdb_semaphore = asyncio.Semaphore(TMDB_MAX_CONCURRENT_REQUESTS)
        _tmdb_semaphore_loop = loop
    return _tmdb_semaphore


def _cached_response(key: str, allow_stale: bool = False) -> Optional[dict]:
    entry = TMDB_CACHE.get(key)
    if entry is None:
        return None
    fresh_until, stale_until, data = entry
    now = time.monotonic()
    if fresh_until > now:
        return deepcopy(data)
    if allow_stale and stale_until > now:
        return deepcopy(data)
    if stale_until <= now:
        TMDB_CACHE.pop(key, None)
    return None


def _store_cached_response(key: str, data: dict, fresh_ttl: float, stale_ttl: float) -> None:
    now = time.monotonic()
    if key not in TMDB_CACHE and len(TMDB_CACHE) >= 512:
        for expired_key, (_, stale_until, _) in list(TMDB_CACHE.items()):
            if stale_until <= now:
                TMDB_CACHE.pop(expired_key, None)
        while len(TMDB_CACHE) >= 512:
            TMDB_CACHE.pop(next(iter(TMDB_CACHE)))
    TMDB_CACHE[key] = (now + fresh_ttl, now + stale_ttl, deepcopy(data))


def _use_stale_response(key: str, path: str, reason: str) -> Optional[dict]:
    stale_data = _cached_response(key, allow_stale=True)
    if stale_data is not None:
        logger.warning("Serving stale TMDB cache path=%s reason=%s", path, reason)
    return stale_data


async def tmdb_get(path: str, params: Optional[dict] = None) -> dict:
    require_tmdb_key()
    request_params = dict(params or {})
    key = _cache_key(path, request_params)
    cached = _cached_response(key)
    if cached is not None:
        return cached
    fresh_ttl, stale_ttl = _cache_policy(path)
    if _tmdb_client is None or _tmdb_client.is_closed:
        raise HTTPException(status_code=503, detail="TMDB HTTP client is not initialized")

    last_error: Optional[BaseException] = None
    last_status: Optional[int] = None
    for attempt in range(TMDB_MAX_ATTEMPTS):
        retry_delay: Optional[float] = None
        try:
            async with _request_semaphore():
                response = await _tmdb_client.get(
                    f"{TMDB_BASE}{path}",
                    params={**request_params, "api_key": TMDB_API_KEY},
                )
        except httpx.TransportError as exc:
            last_error = exc
            _log_tmdb_failure(path, None, f"{type(exc).__name__}: {_safe_diagnostic_text(str(exc))}")
            if attempt + 1 < TMDB_MAX_ATTEMPTS:
                retry_delay = random.uniform(0.0, TMDB_BACKOFF_BASE * (2 ** attempt))
            else:
                stale = _use_stale_response(key, path, type(exc).__name__)
                if stale is not None:
                    return stale
                if isinstance(exc, httpx.TimeoutException):
                    raise HTTPException(status_code=504, detail="TMDB request timed out. Please try again.") from exc
                raise HTTPException(status_code=502, detail="Could not reach TMDB. Please try again.") from exc
        else:
            last_status = response.status_code
            if response.status_code == 200:
                try:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("TMDB response JSON was not an object")
                except ValueError as exc:
                    _log_tmdb_failure(path, response.status_code, response.text)
                    stale = _use_stale_response(key, path, "malformed_json")
                    if stale is not None:
                        return stale
                    raise HTTPException(status_code=502, detail="TMDB returned an invalid response.") from exc
                if fresh_ttl:
                    _store_cached_response(key, data, fresh_ttl, stale_ttl)
                return data

            _log_tmdb_failure(path, response.status_code, response.text)
            if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
                last_error = httpx.HTTPStatusError(
                    f"TMDB returned HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
                if attempt + 1 < TMDB_MAX_ATTEMPTS:
                    retry_delay = _retry_after(response)
                    if retry_delay is None:
                        retry_delay = random.uniform(0.0, TMDB_BACKOFF_BASE * (2 ** attempt))
                else:
                    stale = _use_stale_response(key, path, f"http_{response.status_code}")
                    if stale is not None:
                        return stale
                    if response.status_code == 429:
                        raise HTTPException(status_code=503, detail="TMDB is rate limiting requests. Please retry shortly.")
                    raise HTTPException(status_code=502, detail="TMDB is temporarily unavailable. Please retry shortly.")
            else:
                if response.status_code == 401:
                    detail = "TMDB authentication failed. Verify the server API key configuration."
                else:
                    detail = f"TMDB rejected the request with HTTP {response.status_code}."
                raise HTTPException(status_code=502, detail=detail)

        if retry_delay is not None:
            await asyncio.sleep(retry_delay)

    if isinstance(last_error, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="TMDB request timed out. Please try again.")
    raise HTTPException(
        status_code=502,
        detail=f"TMDB request failed with HTTP {last_status}." if last_status else "Could not reach TMDB.",
    )


def card_from_tmdb(movie: dict) -> MovieCard:
    return MovieCard(
        tmdb_id=int(movie["id"]),
        title=movie.get("title") or movie.get("name") or "Untitled",
        poster_url=image_url(movie.get("poster_path")),
        release_date=movie.get("release_date") or movie.get("first_air_date"),
        vote_average=movie.get("vote_average"),
    )


async def cards_from_results(results: list[dict], limit: int = 24) -> list[MovieCard]:
    return [card_from_tmdb(item) for item in (results or [])[:limit] if item.get("id")]


async def get_movie_details(movie_id: int) -> MovieDetails:
    data = await tmdb_get(f"/movie/{movie_id}", {"language": "en-US"})
    return MovieDetails(
        tmdb_id=int(data["id"]), title=data.get("title") or "Untitled",
        overview=data.get("overview"), release_date=data.get("release_date"),
        poster_url=image_url(data.get("poster_path")),
        backdrop_url=(f"https://image.tmdb.org/t/p/w1280{data['backdrop_path']}" if data.get("backdrop_path") else None),
        vote_average=data.get("vote_average"), genres=data.get("genres") or [],
    )


async def search_tmdb(query: str, page: int = 1) -> dict:
    return await tmdb_get("/search/movie", {"query": query, "include_adult": "false", "language": "en-US", "page": page})


async def first_tmdb_match(query: str) -> Optional[dict]:
    data = await search_tmdb(query, 1)
    results = data.get("results") or []
    return results[0] if results else None


def tfidf_recommend_titles(title: str, top_n: int = 12) -> list[tuple[str, float]]:
    if tfidf_load_error or df is None or tfidf_matrix is None:
        raise HTTPException(status_code=503, detail=tfidf_load_error or "TF-IDF resources are not loaded")
    index = title_to_idx.get(norm_title(title))
    if index is None:
        raise HTTPException(status_code=404, detail=f"'{title}' was not found in the local movie dataset")
    vector = tfidf_matrix[index]
    scores = (tfidf_matrix @ vector.T).toarray().ravel()
    order = np.argsort(-scores)
    recommendations: list[tuple[str, float]] = []
    for idx in order:
        if int(idx) == int(index):
            continue
        try:
            movie_title = str(df.iloc[int(idx)]["title"])
        except (IndexError, KeyError, TypeError):
            continue
        recommendations.append((movie_title, float(scores[int(idx)])))
        if len(recommendations) >= top_n:
            break
    return recommendations


async def poster_for_title(title: str) -> Optional[MovieCard]:
    try:
        match = await first_tmdb_match(title)
        return card_from_tmdb(match) if match else None
    except (HTTPException, KeyError, TypeError, ValueError) as exc:
        logger.warning("Could not resolve poster metadata for a recommendation (%s)", type(exc).__name__)
        return None


@app.get("/api/health")
@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "tmdb_configured": bool(TMDB_API_KEY), "tfidf_ready": tfidf_load_error is None, "tfidf_error": tfidf_load_error}


@app.get("/api/home", response_model=list[MovieCard])
@app.get("/home", response_model=list[MovieCard], include_in_schema=False)
async def home(category: str = Query("trending"), limit: int = Query(24, ge=1, le=50)):
    if category == "trending":
        data = await tmdb_get("/trending/movie/day", {"language": "en-US"})
    elif category in {"popular", "top_rated", "upcoming", "now_playing"}:
        data = await tmdb_get(f"/movie/{category}", {"language": "en-US", "page": 1})
    else:
        raise HTTPException(status_code=400, detail="Invalid category")
    return await cards_from_results(data.get("results", []), limit)


@app.get("/api/search")
@app.get("/tmdb/search", include_in_schema=False)
async def movie_search(query: str = Query(..., min_length=1), page: int = Query(1, ge=1, le=10)):
    return await search_tmdb(query.strip(), page)


@app.get("/api/movie/{tmdb_id}", response_model=MovieDetails)
@app.get("/movie/id/{tmdb_id}", response_model=MovieDetails, include_in_schema=False)
async def movie_details(tmdb_id: int):
    return await get_movie_details(tmdb_id)


@app.get("/api/recommend/genre", response_model=list[MovieCard])
@app.get("/recommend/genre", response_model=list[MovieCard], include_in_schema=False)
async def recommend_genre(tmdb_id: int = Query(...), limit: int = Query(18, ge=1, le=50)):
    details = await get_movie_details(tmdb_id)
    if not details.genres:
        return []
    data = await tmdb_get("/discover/movie", {"with_genres": details.genres[0]["id"], "language": "en-US", "sort_by": "popularity.desc", "page": 1})
    cards = await cards_from_results(data.get("results", []), limit + 1)
    return [card for card in cards if card.tmdb_id != tmdb_id][:limit]


@app.get("/api/recommend/tfidf")
@app.get("/recommend/tfidf", include_in_schema=False)
def recommend_tfidf(title: str = Query(..., min_length=1), top_n: int = Query(10, ge=1, le=50)):
    return [{"title": title, "score": score} for title, score in tfidf_recommend_titles(title, top_n)]


@app.get("/api/movie/{tmdb_id}/recommendations", response_model=RecommendationBundle)
async def movie_recommendations(tmdb_id: int, tfidf_top_n: int = Query(12, ge=1, le=30), genre_limit: int = Query(12, ge=1, le=30)):
    details = await get_movie_details(tmdb_id)
    tfidf_items: list[TFIDFRecommendation] = []
    try:
        local_recs = tfidf_recommend_titles(details.title, tfidf_top_n)
    except HTTPException:
        local_recs = []
    # The shared semaphore bounds these concurrent lookups; missing posters do not remove matches.
    posters = await asyncio.gather(*(poster_for_title(title) for title, _ in local_recs))
    for (title, score), poster in zip(local_recs, posters):
        tfidf_items.append(TFIDFRecommendation(title=title, score=score, tmdb=poster))

    genre_items: list[MovieCard] = []
    if details.genres:
        try:
            data = await tmdb_get("/discover/movie", {"with_genres": details.genres[0]["id"], "language": "en-US", "sort_by": "popularity.desc", "page": 1})
            cards = await cards_from_results(data.get("results", []), genre_limit + 1)
            genre_items = [card for card in cards if card.tmdb_id != tmdb_id][:genre_limit]
        except HTTPException as exc:
            logger.warning("Genre recommendations unavailable for movie_id=%s: %s", tmdb_id, exc.detail)
    return RecommendationBundle(query=details.title, movie_details=details, tfidf_recommendations=tfidf_items, genre_recommendations=genre_items)


# Backward-compatible route used by the original project.
@app.get("/movie/search", response_model=RecommendationBundle, include_in_schema=False)
async def legacy_recommendations(query: str = Query(..., min_length=1), tfidf_top_n: int = Query(12, ge=1, le=30), genre_limit: int = Query(12, ge=1, le=30)):
    match = await first_tmdb_match(query)
    if not match:
        raise HTTPException(status_code=404, detail=f"No TMDB movie found for query: {query}")
    return await movie_recommendations(int(match["id"]), tfidf_top_n, genre_limit)
