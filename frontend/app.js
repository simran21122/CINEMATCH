const API = '';
const $ = (selector) => document.querySelector(selector);
const state = {
  category: 'trending',
  searchResults: [],
  currentMovie: null,
  searchTimer: null,
  requestId: 0,
  searchController: null,
  searchQuery: '',
  searchPromise: null,
  searchCache: new Map(),
  homeRequests: new Map()
};
const titleCase = (value) => value.replaceAll('_', ' ').replace(/\b\w/g, (c) => c.toUpperCase());
const yearOf = (date) => date && /^\d{4}/.test(date) ? date.slice(0, 4) : 'Release date unavailable';
const escapeHTML = (value = '') => String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

async function api(path, {signal, timeoutMs = 40000} = {}) {
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', abortFromCaller, {once:true});
  }
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${API}${path}`, {signal:controller.signal});
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  } catch (error) {
    if (signal?.aborted) throw error;
    if (controller.signal.aborted) throw new Error('The request timed out. Please try again.');
    if (response) throw error;
    throw new Error('Could not reach CineMatch. Check your connection and try again.');
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', abortFromCaller);
  }
}
function showToast(message) {
  const toast = $('#toast'); toast.textContent = message; toast.classList.add('show');
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.classList.remove('show'), 3500);
}
function posterMarkup(movie, extraClass = '') {
  const title = escapeHTML(movie.title || 'Untitled');
  const poster = movie.poster_url || '';
  const rating = Number(movie.vote_average);
  return `<article class="movie-card ${extraClass}" tabindex="0" role="button" data-movie-id="${Number(movie.tmdb_id) || 0}" aria-label="View ${title}">
    <div class="poster-wrap"><div class="poster-placeholder">✦</div>${poster ? `<img loading="lazy" src="${escapeHTML(poster)}" alt="Poster for ${title}" onerror="this.style.display='none'">` : ''}
    ${Number.isFinite(rating) && rating > 0 ? `<span class="rating">★ ${rating.toFixed(1)}</span>` : ''}<span class="card-open">↗</span></div>
    <div class="movie-title" title="${title}">${title}</div><div class="movie-year">${escapeHTML(yearOf(movie.release_date))}</div></article>`;
}
function renderCards(container, movies, emptyMessage = 'No movies found for this section.') {
  if (!movies || !movies.length) { container.innerHTML = `<div class="empty-state">${escapeHTML(emptyMessage)}</div>`; return; }
  container.innerHTML = movies.map((movie) => posterMarkup(movie)).join('');
}
async function loadHealth() {
  const badge = $('#apiStatus');
  try {
    const data = await api('/api/health');
    if (data.tmdb_configured) { badge.className = 'api-status ok'; badge.lastElementChild.textContent = 'API connected'; }
    else { badge.className = 'api-status bad'; badge.lastElementChild.textContent = 'API key needed'; }
  } catch { badge.className = 'api-status bad'; badge.lastElementChild.textContent = 'API offline'; }
}
async function loadHome(category = state.category) {
  state.category = category; $('#categorySelect').value = category;
  $('#feedTitle').innerHTML = `${escapeHTML(titleCase(category))} <span class="heading-muted">${category === 'trending' ? 'right now' : 'movies'}</span>`;
  $('#homeMovies').innerHTML = '<div class="loading-state">Finding your next watch<span class="loading-dots">•••</span></div>';
  let request = state.homeRequests.get(category);
  if (!request) {
    request = api(`/api/home?category=${encodeURIComponent(category)}&limit=24`);
    state.homeRequests.set(category, request);
    request.finally(() => {
      if (state.homeRequests.get(category) === request) state.homeRequests.delete(category);
    }).catch(() => {});
  }
  try {
    const movies = await request;
    if (state.category === category) renderCards($('#homeMovies'), movies);
  } catch (error) {
    if (state.category === category) $('#homeMovies').innerHTML = `<div class="error-state">${escapeHTML(error.message)}. Check your TMDB API key and connection.</div>`;
  }
}
function renderSuggestions(movies, emptyMessage = 'No matching movies found.') {
  const box = $('#suggestions');
  if (!movies.length) { box.innerHTML = `<div class="suggestion-item">${escapeHTML(emptyMessage)}</div>`; box.classList.remove('hidden'); return; }
  box.innerHTML = movies.slice(0, 7).map(movie => `<div class="suggestion-item" data-movie-id="${Number(movie.id)}"><img class="suggestion-thumb" src="${movie.poster_path ? `https://image.tmdb.org/t/p/w92${escapeHTML(movie.poster_path)}` : ''}" alt="" onerror="this.style.visibility='hidden'"><span>${escapeHTML(movie.title || 'Untitled')}</span><span class="suggestion-year">${escapeHTML(yearOf(movie.release_date))}</span></div>`).join('');
  box.classList.remove('hidden');
}
function searchCacheKey(query) { return query.trim().toLocaleLowerCase(); }
function getSearchData(query) {
  const key = searchCacheKey(query);
  const cached = state.searchCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return Promise.resolve(cached.data);
  if (cached) state.searchCache.delete(key);
  if (state.searchQuery === key && state.searchPromise) return state.searchPromise;
  const controller = new AbortController();
  state.searchQuery = key;
  state.searchController = controller;
  const promise = api(`/api/search?query=${encodeURIComponent(query.trim())}`, {signal:controller.signal})
    .then(data => {
      if (state.searchCache.size >= 100) state.searchCache.delete(state.searchCache.keys().next().value);
      state.searchCache.set(key, {data, expiresAt:Date.now() + 30000});
      return data;
    })
    .finally(() => {
      if (state.searchQuery === key) {
        state.searchQuery = '';
        state.searchPromise = null;
        state.searchController = null;
      }
    });
  state.searchPromise = promise;
  return promise;
}
function cancelObsoleteSearch(query = '') {
  const key = searchCacheKey(query);
  if (state.searchQuery && state.searchQuery !== key) state.searchController?.abort();
}
async function doSearch(query) {
  const clean = query.trim();
  if (clean.length < 2) { $('#suggestions').classList.add('hidden'); return; }
  const requestId = ++state.requestId;
  cancelObsoleteSearch(clean);
  const box = $('#suggestions');
  box.innerHTML = '<div class="suggestion-item">Searching movies…</div>';
  box.classList.remove('hidden');
  try {
    const data = await getSearchData(clean);
    if (requestId !== state.requestId) return;
    const results = (data.results || []).filter(m => m.id && m.title);
    state.searchResults = results;
    renderSuggestions(results);
  } catch (error) {
    if (requestId === state.requestId && error.name !== 'AbortError') {
      renderSuggestions([], `Search unavailable: ${error.message}`);
    }
  }
}
function showSearchResults(query, results) {
  $('#searchTerm').textContent = `“${query.trim()}”`;
  $('#searchResultsSection').classList.remove('hidden');
  renderCards($('#searchResults'), results.map(card => ({tmdb_id:card.id,title:card.title,poster_url:card.poster_path?`https://image.tmdb.org/t/p/w500${card.poster_path}`:null,release_date:card.release_date,vote_average:card.vote_average})), 'No matching movies found. Try a different title.');
  $('#searchResultsSection').scrollIntoView({behavior:'smooth',block:'start'});
}
async function submitSearch(query) {
  const requestId = ++state.requestId;
  cancelObsoleteSearch(query);
  $('#searchResultsSection').classList.remove('hidden');
  $('#searchTerm').textContent = `“${query.trim()}”`;
  $('#searchResults').innerHTML = '<div class="loading-state">Searching movies<span class="loading-dots">•••</span></div>';
  try {
    const data = await getSearchData(query);
    if (requestId !== state.requestId) return;
    const results = (data.results || []).filter(movie => movie.id && movie.title);
    state.searchResults = results;
    showSearchResults(query, results);
  } catch (error) {
    if (requestId === state.requestId && error.name !== 'AbortError') {
      $('#searchResults').innerHTML = `<div class="error-state">${escapeHTML(error.message)}. Please try again.</div>`;
    }
  }
}
async function openMovie(id) {
  if (!id) return;
  $('#homeView').classList.add('hidden'); $('#detailView').classList.remove('hidden');
  $('#detailContent').innerHTML = '<div class="loading-state">Loading movie details<span class="loading-dots">•••</span></div>';
  history.pushState({movieId:id}, '', `#movie-${id}`);
  window.scrollTo({top:0,behavior:'smooth'});
  try {
    const movie = await api(`/api/movie/${id}`); state.currentMovie = movie;
    const backdrop = movie.backdrop_url ? `url('${movie.backdrop_url}')` : 'none';
    $('#detailContent').innerHTML = `<section class="detail-hero"><div class="detail-backdrop" style="background-image:${backdrop}"></div><div class="detail-shade"></div>${movie.poster_url ? `<img class="detail-poster" src="${escapeHTML(movie.poster_url)}" alt="Poster for ${escapeHTML(movie.title)}">` : '<div class="detail-poster poster-placeholder">✦</div>'}<div class="detail-copy"><p class="eyebrow"><span class="eyebrow-line"></span> MOVIE DETAILS</p><h1>${escapeHTML(movie.title)}</h1><div class="detail-meta"><span>◷ ${escapeHTML(yearOf(movie.release_date))}</span>${movie.vote_average ? `<span>★ ${Number(movie.vote_average).toFixed(1)} / 10</span>` : ''}<span>TMDB #${Number(movie.tmdb_id)}</span></div><div class="genre-tags">${(movie.genres||[]).map(g=>`<span class="genre-tag">${escapeHTML(g.name)}</span>`).join('')}</div><h3>Overview</h3><p class="overview">${escapeHTML(movie.overview || 'No overview is available for this movie.')}</p></div></section><section class="detail-section"><div class="section-heading"><div><p class="eyebrow">CONTENT-BASED MATCHES</p><h2>Similar movies</h2></div><span class="score-pill">TF-IDF similarity</span></div><div id="tfidfMovies" class="movie-grid"><div class="loading-state">Finding similar stories<span class="loading-dots">•••</span></div></div></section><section class="detail-section"><div class="section-heading"><div><p class="eyebrow">SAME VIBE, NEW STORY</p><h2>More in this genre</h2></div><span class="score-pill">Genre discovery</span></div><div id="genreMovies" class="movie-grid"><div class="loading-state">Exploring genres<span class="loading-dots">•••</span></div></div></section>`;
    loadRecommendations(movie);
  } catch (error) {
    $('#detailContent').innerHTML = `<div class="error-state">${escapeHTML(error.message)}. Please return to discovery and try again.</div>`;
  }
}
async function loadRecommendations(movie) {
  try {
    const bundle = await api(`/api/movie/${movie.tmdb_id}/recommendations?tfidf_top_n=12&genre_limit=12`);
    const tfidf = (bundle.tfidf_recommendations || []).map(item => item.tmdb
      ? {...item.tmdb,title:item.tmdb.title || item.title}
      : {tmdb_id:0,title:item.title});
    renderCards($('#tfidfMovies'), tfidf, 'This title is not in the local TF-IDF dataset. Try another movie for content-based matches.');
    renderCards($('#genreMovies'), bundle.genre_recommendations || [], 'No genre recommendations are available.');
  } catch (error) {
    $('#tfidfMovies').innerHTML = `<div class="empty-state">Content-based matches unavailable: ${escapeHTML(error.message)}</div>`;
    try { const genre = await api(`/api/recommend/genre?tmdb_id=${movie.tmdb_id}&limit=12`); renderCards($('#genreMovies'), genre); }
    catch (genreError) { $('#genreMovies').innerHTML = `<div class="empty-state">${escapeHTML(genreError.message)}</div>`; }
  }
}
function goHome(updateHistory = true) {
  $('#detailView').classList.add('hidden'); $('#homeView').classList.remove('hidden');
  $('#suggestions').classList.add('hidden');
  if (updateHistory) history.pushState({}, '', '#home');
  window.scrollTo({top:0,behavior:'smooth'});
}
function setActiveNav(link) {
  document.querySelectorAll('.nav-link').forEach(item => item.classList.toggle('active', item === link));
}

$('#searchInput').addEventListener('input', event => {
  clearTimeout(state.searchTimer);
  const value = event.target.value;
  state.requestId++;
  cancelObsoleteSearch(value);
  if (value.trim().length < 2) { $('#suggestions').classList.add('hidden'); return; }
  state.searchTimer = setTimeout(() => doSearch(value), 350);
});
$('#searchForm').addEventListener('submit', event => {
  event.preventDefault();
  clearTimeout(state.searchTimer);
  const query = $('#searchInput').value.trim();
  if (query.length < 2) { showToast('Type at least two characters to search.'); return; }
  $('#suggestions').classList.add('hidden');
  submitSearch(query);
});
$('#suggestions').addEventListener('click', event => { const item = event.target.closest('[data-movie-id]'); if (item) openMovie(Number(item.dataset.movieId)); });
$('#homeMovies').addEventListener('click', event => { const card = event.target.closest('[data-movie-id]'); if (card) openMovie(Number(card.dataset.movieId)); });
$('#searchResults').addEventListener('click', event => { const card = event.target.closest('[data-movie-id]'); if (card) openMovie(Number(card.dataset.movieId)); });
$('#detailContent').addEventListener('click', event => { const card = event.target.closest('[data-movie-id]'); if (card) openMovie(Number(card.dataset.movieId)); });
for (const container of ['#homeMovies','#searchResults','#detailContent']) $(container).addEventListener('keydown', event => { if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('.movie-card')) { event.preventDefault(); openMovie(Number(event.target.dataset.movieId)); } });
$('#categorySelect').addEventListener('change', event => loadHome(event.target.value));
$('#refreshFeed').addEventListener('click', () => loadHome(state.category));
$('#clearSearch').addEventListener('click', () => {
  $('#searchInput').value='';
  state.requestId++;
  cancelObsoleteSearch('');
  $('#suggestions').classList.add('hidden');
  $('#searchResultsSection').classList.add('hidden');
  $('#searchInput').focus();
});
$('#backButton').addEventListener('click', () => goHome());
$('#homeNav').addEventListener('click', event => { event.preventDefault(); setActiveNav(event.currentTarget); goHome(); });
document.querySelectorAll('[data-category]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); setActiveNav(event.currentTarget); goHome(false); loadHome(link.dataset.category); $('#homeMovies').scrollIntoView({behavior:'smooth',block:'start'}); }));
document.addEventListener('click', event => {
  if (!event.target.closest('.search-form')) {
    $('#suggestions').classList.add('hidden');
    state.requestId++;
    cancelObsoleteSearch('');
  }
});
window.addEventListener('popstate', event => { if (event.state?.movieId) openMovie(event.state.movieId); else if (location.hash.startsWith('#movie-')) openMovie(Number(location.hash.replace('#movie-',''))); else goHome(false); });

loadHealth();
if (location.hash.startsWith('#movie-')) openMovie(Number(location.hash.replace('#movie-',''))); else loadHome('trending');
