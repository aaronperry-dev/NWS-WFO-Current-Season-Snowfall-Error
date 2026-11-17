<?php
declare(strict_types=1);
/**
 * Viewer configuration.
 * DATA_DIR holds manifest.json, cwa.geojson, <season>/<OFFICE>_mean_error.bin and <season>/events/<OFFICE>_<id>_error.bin, as written by pipeline/update_season.py.
 * Override with the NWS_VIEWER_DATA environment variable to keep the data outside the web root.
 */
const APP_TITLE = 'NWS WFO Current-Season Snowfall Forecast Error';
$dataDir = getenv('NWS_VIEWER_DATA') ?: __DIR__ . '/data';
define('DATA_DIR', rtrim($dataDir, '/'));

/** Parsed manifest.json, or null if the exporter has not been run yet. */
function load_manifest(): ?array {
    $f = DATA_DIR . '/manifest.json';
    if (!is_file($f)) return null;
    $m = json_decode((string) file_get_contents($f), true);
    return is_array($m) ? $m : null;
}
