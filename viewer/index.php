<?php
declare(strict_types=1);
require __DIR__ . '/config.php';
$manifest = load_manifest();
$v = fn(string $f): string => (string) @filemtime(__DIR__ . '/' . $f);      // cache-busting version for assets
$h = fn(string $s): string => htmlspecialchars($s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
?><!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= $h(APP_TITLE) ?></title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="vendor/leaflet/leaflet.css">
<link rel="stylesheet" href="assets/style.css?v=<?= $v('assets/style.css') ?>">
</head>
<body>
<header class="topbar">
  <h1><?= $h(APP_TITLE) ?></h1>
  <p>NWS Boston/Norton &middot; Gray/Portland &middot; Caribou &middot; Burlington &middot; <span id="topSeason"></span></p>
</header>
<div class="app">
  <div id="map" role="application" aria-label="Map of snowfall forecast error by NWS office, season to date and by event"></div>
  <aside class="panel">
    <header>
      <p class="sub">How well the official WFO snowfall forecasts did in each snow event this winter: forecast − observed, season to date and event by event, on the native ~4&nbsp;km grid of the observations.</p>
    </header>
    <div id="notice" class="notice" hidden></div>
    <section class="controls">
      <label>Season <select id="season"></select></label>
      <label>Basemap <select id="basemap"></select></label>
      <label>Overlay opacity <input id="opacity" type="range" min="0" max="1" step="0.05" value="0.85"> <output id="opacityOut">85%</output></label>
    </section>
    <section id="status" class="status"></section>
    <section>
      <h2>Offices</h2>
      <table id="offices" class="offices"></table>
      <p class="hint">Tick to show or hide an office. Click a row (or a CWA on the map) for details and to list only its events.</p>
    </section>
    <section id="eventsSection" hidden>
      <h2 id="eventsTitle">Events</h2>
      <div class="evwrap"><table id="events" class="events"></table></div>
      <p class="hint">Obs. / Fcst.: CWA-mean inches. Click a verified event to map its error; click it again, or press Esc, to return.</p>
    </section>
    <section id="detail" class="detail" hidden></section>
    <section class="about">
      <h2>How to read this</h2>
      <ul>
        <li><b>Red</b> = the forecast was too high; <b>blue</b> = too low. Each cell is forecast − observed snowfall in inches. The map shows the season-to-date mean over all verified events (color range ±2&nbsp;in), or one event when you pick it (±6&nbsp;in).</li>
        <li><b>Event</b>: a stretch of snow with a CWA-mean total of at least 1.5&nbsp;in (6-h periods of 0.08&nbsp;in or more, gaps up to 12&nbsp;h merged, split at 48&nbsp;h). Snow that stays local also counts when at least 5% of the CWA gets 1&nbsp;in or more; these are tagged <b>local</b>. Times are UTC.</li>
        <li>Forecast: the official WFO-edited NDFD snowfall grid, the latest run issued at least 12&nbsp;h before the event began. Observed: NOHRSC National Snowfall Analysis.</li>
        <li>The public forecast archive runs about 5&nbsp;days behind, so a finished event waits as &ldquo;awaiting forecast archive&rdquo; until it can be verified. Recent analyses can still be revised; they are re-read for 21&nbsp;days.</li>
        <li>A few events say little: season means from fewer than 5&nbsp;events are flagged &#9888;.</li>
        <li>Hatched cells are excluded radar beam-blockage artifacts in the observation analysis.</li>
      </ul>
    </section>
  </aside>
</div>
<?php if ($manifest !== null): ?>
<script id="manifest" type="application/json"><?= json_encode($manifest, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_UNESCAPED_SLASHES) ?></script>
<?php endif; ?>
<script>window.NWS = { api: 'api.php' };</script>
<script src="vendor/leaflet/leaflet.js"></script>
<script src="assets/app.js?v=<?= $v('assets/app.js') ?>"></script>
<noscript><p style="padding:1rem">This viewer needs JavaScript.</p></noscript>
</body>
</html>
