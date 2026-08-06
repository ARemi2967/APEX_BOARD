/* Thin fetch wrapper for the dashboard API. All calls resolve to JSON and
   throw an Error with the upstream message on non-2xx. */
const API = (() => {
  const base = "/api";

  async function json(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    let payload = null;
    try { payload = await resp.json(); } catch (_) { /* non-JSON body */ }
    if (!resp.ok) {
      const msg = payload?.error?.message || resp.statusText || `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    return payload;
  }

  return {
    listPlayers: () => json(`${base}/players`),
    getCurrent: (id) => json(`${base}/players/${id}/current`),
    getHistory: (id, metric, days = 7) =>
      json(`${base}/players/${id}/history?metric=${encodeURIComponent(metric)}&days=${days}`),
    getLegends: (id) => json(`${base}/players/${id}/legends`),
    getDeltas: (id, days = 1) => json(`${base}/players/${id}/deltas?days=${days}`),
    getBreakdown: (id) => json(`${base}/players/${id}/breakdown`),
    getLegendActivity: (id, days = 1) => json(`${base}/players/${id}/legend-activity?days=${days}`),
    getLegendDaily: (id, days = 7) => json(`${base}/players/${id}/legend-daily?days=${days}`),
  };
})();
