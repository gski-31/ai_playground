/**
 * Restaurant Roulette — Main Application
 * Uses Google Maps JavaScript API (Places library) to find nearby restaurants
 * and presents a random selection via a slot-machine style animation.
 */

(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────────
  const state = {
    userLat: null,
    userLng: null,
    locationReady: false,
    restaurants: [],
    excludedCuisines: [],
    minRating: 3.5,
    distanceMiles: 10,
    activePriceLevels: [1, 2, 3, 4],
    spinning: false,
    placesService: null,
    map: null,
  };

  // ── DOM refs ───────────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const distanceSlider  = $('#distance-slider');
  const distanceValue   = $('#distance-value');
  const ratingSlider    = $('#rating-slider');
  const ratingValue     = $('#rating-value');
  const excludeInput    = $('#exclude-input');
  const excludeTags     = $('#exclude-tags');
  const spinBtn         = $('#spin-btn');
  const respinBtn       = $('#respin-btn');
  const adjustFiltersBtn = $('#adjust-filters-btn');
  const locationStatus  = $('#location-status');
  const filtersSection  = $('#filters');
  const rouletteSection = $('#roulette-section');
  const rouletteStrip   = $('#roulette-strip');
  const resultSection   = $('#result-section');
  const noResults       = $('#no-results');
  const loadingOverlay  = $('#loading-overlay');

  // ── Initialize (called by Google Maps callback) ────────────────────────
  window.initApp = function () {
    // Create a hidden map div required by PlacesService
    const mapDiv = document.createElement('div');
    mapDiv.style.display = 'none';
    document.body.appendChild(mapDiv);
    state.map = new google.maps.Map(mapDiv);
    state.placesService = new google.maps.places.PlacesService(state.map);

    bindEvents();
    requestGeolocation();
  };

  // ── Geolocation ────────────────────────────────────────────────────────
  function requestGeolocation() {
    if (!navigator.geolocation) {
      locationStatus.textContent = '⚠️ Geolocation is not supported by your browser.';
      return;
    }

    navigator.geolocation.getCurrentPosition(
      (pos) => {
        state.userLat = pos.coords.latitude;
        state.userLng = pos.coords.longitude;
        state.locationReady = true;
        spinBtn.disabled = false;
        locationStatus.textContent = '📍 Location detected! Ready to spin.';
      },
      (err) => {
        locationStatus.textContent = '⚠️ Location access denied. Please allow location access and refresh.';
        console.error('Geolocation error:', err);
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
  }

  // ── Event Bindings ─────────────────────────────────────────────────────
  function bindEvents() {
    // Sliders
    distanceSlider.addEventListener('input', () => {
      state.distanceMiles = parseFloat(distanceSlider.value);
      distanceValue.textContent = state.distanceMiles;
    });

    ratingSlider.addEventListener('input', () => {
      state.minRating = parseFloat(ratingSlider.value);
      ratingValue.textContent = state.minRating;
    });

    // Exclude cuisine tags
    excludeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && excludeInput.value.trim()) {
        e.preventDefault();
        addExcludeTag(excludeInput.value.trim().toLowerCase());
        excludeInput.value = '';
      }
    });

    // Price level buttons
    document.querySelectorAll('.price-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        btn.classList.toggle('active');
        state.activePriceLevels = Array.from(document.querySelectorAll('.price-btn.active'))
          .map((b) => parseInt(b.dataset.price));
      });
    });

    // Spin / Respin
    spinBtn.addEventListener('click', handleSpin);
    respinBtn.addEventListener('click', handleSpin);
    adjustFiltersBtn.addEventListener('click', () => {
      noResults.classList.add('hidden');
      filtersSection.scrollIntoView({ behavior: 'smooth' });
    });
  }

  // ── Exclude Tags ───────────────────────────────────────────────────────
  function addExcludeTag(text) {
    if (state.excludedCuisines.includes(text)) return;
    state.excludedCuisines.push(text);

    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.innerHTML = `${text} <button aria-label="Remove">&times;</button>`;
    tag.querySelector('button').addEventListener('click', () => {
      state.excludedCuisines = state.excludedCuisines.filter((c) => c !== text);
      tag.remove();
    });
    excludeTags.appendChild(tag);
  }

  // ── Search & Spin ──────────────────────────────────────────────────────
  async function handleSpin() {
    if (state.spinning || !state.locationReady) return;
    state.spinning = true;

    // Hide previous results
    resultSection.classList.add('hidden');
    rouletteSection.classList.add('hidden');
    noResults.classList.add('hidden');
    loadingOverlay.classList.remove('hidden');

    try {
      const restaurants = await fetchRestaurants();

      if (restaurants.length === 0) {
        loadingOverlay.classList.add('hidden');
        noResults.classList.remove('hidden');
        state.spinning = false;
        return;
      }

      state.restaurants = restaurants;
      loadingOverlay.classList.add('hidden');
      runRouletteAnimation(restaurants);
    } catch (err) {
      console.error('Error fetching restaurants:', err);
      loadingOverlay.classList.add('hidden');
      locationStatus.textContent = '⚠️ Error searching for restaurants. Please try again.';
      state.spinning = false;
    }
  }

  // ── Google Places API: Nearby Search ───────────────────────────────────
  function fetchRestaurants() {
    return new Promise((resolve, reject) => {
      const radiusMeters = state.distanceMiles * 1609.34;
      // Google Places caps radius at 50,000 m
      const clampedRadius = Math.min(radiusMeters, 50000);

      const request = {
        location: new google.maps.LatLng(state.userLat, state.userLng),
        radius: clampedRadius,
        type: 'restaurant',
      };

      state.placesService.nearbySearch(request, (results, status, pagination) => {
        if (status === google.maps.places.PlacesServiceStatus.OK) {
          let filtered = filterResults(results);

          // If we have pagination and need more results, fetch next page
          if (filtered.length < 5 && pagination && pagination.hasNextPage) {
            pagination.nextPage(); // triggers another callback
            // Wait for next page by wrapping in a second call
            setTimeout(() => {
              state.placesService.nearbySearch(request, (moreResults, moreStatus) => {
                if (moreStatus === google.maps.places.PlacesServiceStatus.OK) {
                  filtered = filtered.concat(filterResults(moreResults));
                  // Remove duplicates by place_id
                  const seen = new Set();
                  filtered = filtered.filter((r) => {
                    if (seen.has(r.place_id)) return false;
                    seen.add(r.place_id);
                    return true;
                  });
                }
                resolve(filtered);
              });
            }, 2000);
          } else {
            resolve(filtered);
          }
        } else if (status === google.maps.places.PlacesServiceStatus.ZERO_RESULTS) {
          resolve([]);
        } else {
          reject(new Error('Places API error: ' + status));
        }
      });
    });
  }

  function filterResults(results) {
    return results.filter((place) => {
      // Rating filter
      if (place.rating && place.rating < state.minRating) return false;
      if (!place.rating) return false; // skip unrated

      // Price level filter
      if (place.price_level !== undefined && !state.activePriceLevels.includes(place.price_level)) {
        return false;
      }

      // Exclude cuisines — check name and types
      const nameAndTypes = (place.name + ' ' + (place.types || []).join(' ')).toLowerCase();
      for (const excluded of state.excludedCuisines) {
        if (nameAndTypes.includes(excluded)) return false;
      }

      return true;
    });
  }

  // ── Roulette Animation ─────────────────────────────────────────────────
  function runRouletteAnimation(restaurants) {
    // Pick the winner
    const winnerIndex = Math.floor(Math.random() * restaurants.length);
    const winner = restaurants[winnerIndex];

    // Build a long strip of items (repeat restaurants to create scrolling effect)
    const totalItems = 30; // total items in strip before landing on winner
    const items = [];
    for (let i = 0; i < totalItems; i++) {
      items.push(restaurants[i % restaurants.length]);
    }
    items.push(winner); // The final item is the winner

    // Render strip
    rouletteStrip.innerHTML = '';
    rouletteStrip.style.transition = 'none';
    rouletteStrip.style.transform = 'translateY(0)';

    items.forEach((place) => {
      const div = document.createElement('div');
      div.className = 'roulette-item';

      const photoUrl = getPhotoUrl(place, 80);
      div.innerHTML = `
        <img class="roulette-item-photo" src="${photoUrl}" alt="" onerror="this.style.display='none'">
        <div class="roulette-item-info">
          <div class="roulette-item-name">${escapeHtml(place.name)}</div>
          <div class="roulette-item-rating">${renderStars(place.rating)} ${place.rating}</div>
        </div>
      `;
      rouletteStrip.appendChild(div);
    });

    // Show roulette, scroll to it
    rouletteSection.classList.remove('hidden');
    rouletteSection.scrollIntoView({ behavior: 'smooth' });

    // Animate after a brief frame delay
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const itemHeight = 120;
        const targetOffset = totalItems * itemHeight; // land on the last item (winner)
        rouletteStrip.style.transition = 'transform 3s cubic-bezier(0.15, 0.6, 0.35, 1)';
        rouletteStrip.style.transform = `translateY(-${targetOffset}px)`;
      });
    });

    // After animation ends, show result
    setTimeout(() => {
      rouletteSection.classList.add('hidden');
      showResult(winner);
      state.spinning = false;
    }, 3300);
  }

  // ── Show Result ────────────────────────────────────────────────────────
  function showResult(place) {
    const photo = $('#result-photo');
    const noPhoto = $('#result-no-photo');
    const photoUrl = getPhotoUrl(place, 600);

    if (photoUrl && !photoUrl.includes('undefined')) {
      photo.src = photoUrl;
      photo.classList.remove('hidden');
      noPhoto.classList.add('hidden');
    } else {
      photo.classList.add('hidden');
      noPhoto.classList.remove('hidden');
    }

    $('#result-name').textContent = place.name;
    $('#result-rating').innerHTML =
      `<span class="star">${renderStars(place.rating)}</span>` +
      `<span class="rating-number">${place.rating}</span>` +
      `<span class="rating-count">(${place.user_ratings_total || '?'} reviews)</span>`;

    $('#result-address').textContent = place.vicinity || 'Address not available';

    const priceStr = place.price_level ? '$'.repeat(place.price_level) : '';
    const priceEl = $('#result-price');
    priceEl.textContent = priceStr;
    priceEl.classList.toggle('hidden', !priceStr);

    // Directions link
    const dirUrl = `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(place.name)}&destination_place_id=${place.place_id}`;
    $('#result-directions').href = dirUrl;

    resultSection.classList.remove('hidden');
    resultSection.scrollIntoView({ behavior: 'smooth' });
  }

  // ── Helpers ────────────────────────────────────────────────────────────
  function getPhotoUrl(place, maxWidth) {
    if (place.photos && place.photos.length > 0) {
      return place.photos[0].getUrl({ maxWidth });
    }
    return '';
  }

  function renderStars(rating) {
    const full = Math.floor(rating);
    const half = rating % 1 >= 0.5 ? 1 : 0;
    const empty = 5 - full - half;
    return '★'.repeat(full) + (half ? '½' : '') + '☆'.repeat(empty);
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
})();
