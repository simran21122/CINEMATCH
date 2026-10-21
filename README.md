# CineMatch — Movie Recommendation System

A movie discovery application rebuilt with a **HTML/CSS/JavaScript frontend** and a **FastAPI REST API backend**. It preserves the original project's movie feed, TMDB search and details, TF-IDF content-based recommendations, and genre-based recommendations.

## Features

- Responsive dark cinema-inspired UI
- Trending, Popular, Top Rated, Now Playing, and Upcoming movie feeds
- Search with live suggestions and matching results
- Movie details, posters, backdrop, release date, genres, overview, and rating
- Content-based recommendations from the existing TF-IDF matrix
- Genre recommendations via TMDB discover API
- FastAPI health endpoint and interactive API docs
- Uses the existing `df.pkl`, `indices.pkl`, `tfidf.pkl`, and `tfidf_matrix.pkl` artifacts

## Tech stack

- Frontend: HTML5, CSS3, vanilla JavaScript
- Backend: Python, FastAPI, Uvicorn
- Movie data and posters: TMDB API
- Similarity model: scikit-learn TF-IDF artifacts, NumPy, SciPy, Pandas

## Project structure

```text
Movies-Recommendation-System/
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
├── df.pkl
├── indices.pkl
├── tfidf.pkl
└── tfidf_matrix.pkl
```

## Setup on Windows / VS Code

1. Install Python 3.11 (recommended) and VS Code.
2. Extract the project ZIP and open the project folder in VS Code.
3. Open **Terminal → New Terminal**.
4. Create and activate a virtual environment:

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, use Command Prompt and run `venv\Scripts\activate.bat`.

5. Install dependencies:

   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

6. Get a TMDB API key from https://www.themoviedb.org/settings/api.
7. Edit the `.env` file beside `main.py` (create it if needed) and add your key:

   ```env
   TMDB_API_KEY=your_actual_tmdb_api_key
   ```

8. Start the API and frontend together:

   ```powershell
   uvicorn main:app --reload
   ```

9. Open **http://127.0.0.1:8000**. The HTML frontend is served by FastAPI, so you do not need to run a separate frontend server.

API documentation: http://127.0.0.1:8000/docs  
Health check: http://127.0.0.1:8000/api/health

## REST API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | Check API and model status |
| GET | `/api/home?category=trending&limit=24` | Fetch movie feed |
| GET | `/api/search?query=batman` | Search TMDB movies |
| GET | `/api/movie/{tmdb_id}` | Fetch movie details |
| GET | `/api/movie/{tmdb_id}/recommendations` | Return details, TF-IDF matches, and genre matches |
| GET | `/api/recommend/genre?tmdb_id=123&limit=12` | Fetch genre recommendations |
| GET | `/api/recommend/tfidf?title=Toy%20Story&top_n=10` | Inspect local TF-IDF matches |

Supported home categories: `trending`, `popular`, `top_rated`, `now_playing`, and `upcoming`.

## Recommendation logic

The local TF-IDF matrix represents movie text features as vectors. For a selected title, the API finds its row in the saved title index, computes similarity against other rows, sorts the scores, and returns the highest-scoring different titles. TMDB search is used to attach poster metadata to these titles. Genre recommendations use the selected TMDB movie's first genre and the TMDB discover endpoint.

The local dataset does not necessarily contain every movie available through TMDB. If a selected title is missing from the local dataset, the UI can still show genre recommendations while content-based matches may be unavailable.

## Troubleshooting

- **TMDB_API_KEY is not configured:** create `.env` beside `main.py`, add the key, then restart Uvicorn.
- **Model artifacts failed to load:** ensure all four `.pkl` files are in the same directory as `main.py`; use the project's compatible Python/scikit-learn environment.
- **Port 8000 is busy:** run `uvicorn main:app --reload --port 8001` and visit `http://127.0.0.1:8001`.
- **Intermittent TMDB errors:** requests use a shared asynchronous client, bounded concurrency, short-lived successful-response caching, and retries for transient failures. Search and movie feeds can use a previously successful stale result during a temporary upstream outage. Invalid API-key responses are not retried or hidden by stale data.
- **Diagnose a TMDB failure:** inspect the server log for the TMDB path, upstream status, sanitized response body, and transport exception type. API keys and sensitive request headers are not logged.

Run the offline TMDB resilience tests with:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Security note

Never commit `.env` or expose your API key in frontend JavaScript. The key is read by the backend only. Pickle files should only be loaded from trusted sources because Python pickle deserialization can execute code.

## Credits

Movie metadata and images are provided by [The Movie Database (TMDB)](https://www.themoviedb.org/). This project is an independent educational project and is not endorsed by TMDB.
=======
# CINEMATCH
>>>>>>> 0b6f2c3791ecc1e114bdb00aa0ddf2c820976902
