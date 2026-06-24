"""
metrics.py — per-frame FITS star-quality metrics for star_check

Computes (all star-shape / transparency / artifact metrics — NOT focus):
  - Eccentricity        — median across detected stars; 0 = round, → 1 = trailing
  - Elongation ratio    — median b/a axis ratio; 1 = round, → 0 = elongated
  - Trailing angle      — dominant major-axis direction [0°, 180°)
                           0° = horizontal, 90° = vertical
  - Angle consistency   — circular coherence [0, 1]:
                             < 0.3 → random directions (atmospheric turbulence)
                           0.3–0.7 → partially consistent (wind / mixed)
                             > 0.7 → highly consistent (tracking / mount error)
  - Center eccentricity — median ecc for stars in the central image zone
  - Edge eccentricity   — median ecc for stars in the outer image zone
  - Ecc field delta     — edge_ecc − center_ecc; > 0.10 = possible tilt / curvature
  - Sky background      — median ADU
  - Sky RMS             — sky noise level
  - Sky gradient        — (bg_max − bg_min) / bg_median; > 0.30 ≈ cloud cover
  - Star count          — number of detected stars
  - SNR                 — median (peak / sky_rms); transparency proxy
  - Trail count         — detected linear artifacts (satellites, planes, meteors)
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
from astropy.io import fits
from astropy.modeling import fitting, models
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.detection import DAOStarFinder

# Half-size of the star cutout for Gaussian fitting (pixels either side of centroid)
_CUTOUT_HALF: int = 15

# Maximum stars used for morphology calculations
_MAX_STARS: int = 200


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_fits(path: str | Path) -> tuple[np.ndarray, dict]:
    """
    Return (2-D float64 image array, metadata dict).

    Handles multi-extension FITS and mono cubes saved as 1×H×W.
    Raises ValueError if no 2-D image data is found.
    """
    path = Path(path)
    with fits.open(path) as hdul:
        data = None
        header: dict = {}
        for hdu in hdul:
            if hdu.data is None:
                continue
            raw = hdu.data
            if raw.ndim == 3 and raw.shape[0] == 1:
                raw = raw[0]
            if raw.ndim == 2:
                data = raw.astype(np.float64)
                header = dict(hdu.header)
                break
        if data is None:
            raise ValueError(f"No 2-D image data found in {path.name}")

    focallen = header.get("FOCALLEN")
    xpixsz   = header.get("XPIXSZ")
    xbinning = header.get("XBINNING") or 1

    plate_scale = None
    if focallen and xpixsz:
        try:
            ps = (float(xpixsz) * float(xbinning) / float(focallen)) * 206.265
            plate_scale = ps if ps > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    meta = {
        "filename":    path.name,
        "path":        str(path),
        "exptime":     header.get("EXPTIME") or header.get("EXPOSURE"),
        "filter":      header.get("FILTER"),
        "gain":        header.get("GAIN"),
        "ccd_temp":    header.get("CCD-TEMP") or header.get("CCDTEMP"),
        "plate_scale": plate_scale,
    }
    return data, meta


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

def estimate_background(data: np.ndarray) -> tuple[np.ndarray, float, float]:
    """
    Sigma-clipped 2-D background model.

    Returns (background_array, sky_median_ADU, sky_rms_ADU).
    """
    box = max(32, min(data.shape) // 10)
    sigma_clip = SigmaClip(sigma=3.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        bkg = Background2D(
            data,
            box_size=box,
            filter_size=3,
            sigma_clip=sigma_clip,
            bkg_estimator=MedianBackground(),
        )
    return (
        bkg.background,
        float(bkg.background_median),
        float(bkg.background_rms_median),
    )


# ---------------------------------------------------------------------------
# Sky gradient
# ---------------------------------------------------------------------------

def measure_sky_gradient(bkg_array: np.ndarray) -> float:
    """
    Background uniformity ratio: (bg_max − bg_min) / bg_median.

    Near 0    → uniform sky (good)
    0.10–0.30 → mild gradient (moonlight edge, light dome)
      > 0.30  → strong gradient — cloud cover likely
    """
    median = float(np.median(bkg_array))
    if median <= 0:
        return float("nan")
    bg_max = float(bkg_array.max())
    bg_min = float(bkg_array.min())
    return float((bg_max - bg_min) / median)


# ---------------------------------------------------------------------------
# Star detection
# ---------------------------------------------------------------------------

def detect_stars(
    data: np.ndarray,
    bkg_array: np.ndarray,
    sky_rms: float,
    threshold_sigma: float = 5.0,
) -> Optional[object]:
    """
    DAOStarFinder detection on background-subtracted image.

    Returns a photutils source table (sorted brightest-first, capped at
    _MAX_STARS) or None if no stars were detected.
    """
    data_sub = data - bkg_array
    daofind = DAOStarFinder(
        fwhm=4.0,
        threshold=threshold_sigma * sky_rms,
        n_brightest=_MAX_STARS,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sources = daofind(data_sub)
    return sources


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cutout(
    data_sub: np.ndarray, x: float, y: float
) -> tuple[Optional[np.ndarray], int, int]:
    """Extract a square cutout centred on (x, y)."""
    xi, yi = int(round(x)), int(round(y))
    r  = _CUTOUT_HALF
    y0 = max(0, yi - r);  y1 = min(data_sub.shape[0], yi + r + 1)
    x0 = max(0, xi - r);  x1 = min(data_sub.shape[1], xi + r + 1)
    cut = data_sub[y0:y1, x0:x1]
    if cut.shape[0] < 5 or cut.shape[1] < 5:
        return None, 0, 0
    return cut, x0, y0


# ---------------------------------------------------------------------------
# Star morphology  (primary metric block)
# ---------------------------------------------------------------------------

def measure_star_morphology(
    data: np.ndarray,
    bkg_array: np.ndarray,
    sources,
    image_shape: tuple[int, int],
) -> tuple[float, float, float, float, float, float, float]:
    """
    Fit a rotated 2-D Gaussian to each star cutout.

    Returns
    -------
    median_eccentricity : float
        0 = round, → 1 = elongated/trailing.  Primary verdict driver.
    median_elongation : float
        Median b/a axis ratio; 1 = round, → 0 = elongated.
    trailing_angle_deg : float
        Dominant major-axis direction [0°, 180°).
        0° = horizontal (RA if camera aligned), 90° = vertical (Dec).
    angle_consistency : float
        Circular coherence of trailing directions [0, 1].
        < 0.3 = random (atmospheric turbulence / wind from all angles)
        0.3–0.7 = partially consistent (directional wind)
        > 0.7 = highly consistent → likely a single-axis tracking fault
    center_ecc : float
        Median eccentricity for stars in the central image zone.
    edge_ecc : float
        Median eccentricity for stars in the outer image zone.
    ecc_field_delta : float
        edge_ecc − center_ecc.  > 0.10 suggests field tilt or curvature.
    """
    nan = float("nan")
    if sources is None or len(sources) == 0:
        return nan, nan, nan, nan, nan, nan, nan

    data_sub = data - bkg_array
    fitter   = fitting.LevMarLSQFitter()

    h, w       = image_shape
    half_h     = h / 2.0
    half_w     = w / 2.0
    # Stars within this radius of the image centre → "center zone"
    zone_radius = 0.45 * min(half_h, half_w)

    eccs:        list[float] = []
    elongs:      list[float] = []
    angles:      list[float] = []   # major-axis angle in [0°, 180°)
    center_eccs: list[float] = []
    edge_eccs:   list[float] = []

    for row in sources:
        cut, _x0, _y0 = _cutout(data_sub, row["x_centroid"], row["y_centroid"])
        if cut is None:
            continue

        yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
        cy, cx  = cut.shape[0] / 2.0, cut.shape[1] / 2.0
        amp     = float(np.clip(cut.max(), 1.0, None))

        init = models.Gaussian2D(
            amplitude=amp,
            x_mean=cx,    y_mean=cy,
            x_stddev=2.0, y_stddev=2.0,
            theta=0.0,
            bounds={
                "x_stddev":  (0.1, _CUTOUT_HALF),
                "y_stddev":  (0.1, _CUTOUT_HALF),
                "amplitude": (0.0, None),
            },
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fitted = fitter(init, xx, yy, cut)

            sx    = abs(fitted.x_stddev.value)
            sy    = abs(fitted.y_stddev.value)
            theta = fitted.theta.value   # radians; rotation of Gaussian x-axis from image x-axis
            if sx <= 0 or sy <= 0:
                continue

            a     = max(sx, sy)
            b     = min(sx, sy)
            ecc   = float(np.sqrt(max(0.0, 1.0 - (b / a) ** 2)))
            elong = float(b / a)

            # Angle of the MAJOR axis in [0°, 180°)
            # If x_stddev is larger, major axis is along the theta direction.
            # If y_stddev is larger, major axis is perpendicular to theta.
            if sx >= sy:
                angle_deg = float(np.degrees(theta) % 180.0)
            else:
                angle_deg = float((np.degrees(theta) + 90.0) % 180.0)

            eccs.append(ecc)
            elongs.append(elong)
            angles.append(angle_deg)

            # Center / edge zone classification
            star_x  = float(row["x_centroid"])
            star_y  = float(row["y_centroid"])
            dist_sq = (star_x - half_w) ** 2 + (star_y - half_h) ** 2
            if dist_sq < zone_radius ** 2:
                center_eccs.append(ecc)
            else:
                edge_eccs.append(ecc)

        except Exception:
            continue

    if not eccs:
        return nan, nan, nan, nan, nan, nan, nan

    median_ecc   = float(np.median(eccs))
    median_elong = float(np.median(elongs))

    # Circular mean for angles modulo 180° — double-angle trick handles wrap-around
    if len(angles) >= 3:
        ang_rad = np.radians(angles)
        doubled = 2.0 * ang_rad
        mc = float(np.mean(np.cos(doubled)))
        ms = float(np.mean(np.sin(doubled)))
        dominant_angle = float(np.degrees(np.arctan2(ms, mc)) / 2.0 % 180.0)
        consistency    = float(np.sqrt(mc ** 2 + ms ** 2))
    else:
        dominant_angle = float(np.median(angles)) if angles else nan
        consistency    = nan

    center_ecc = float(np.median(center_eccs)) if len(center_eccs) >= 3 else nan
    edge_ecc   = float(np.median(edge_eccs))   if len(edge_eccs)   >= 3 else nan

    if not math.isnan(center_ecc) and not math.isnan(edge_ecc):
        field_delta = float(edge_ecc - center_ecc)
    else:
        field_delta = nan

    return median_ecc, median_elong, dominant_angle, consistency, center_ecc, edge_ecc, field_delta


# ---------------------------------------------------------------------------
# SNR
# ---------------------------------------------------------------------------

def measure_snr(sources, sky_rms: float) -> float:
    """
    Median per-star SNR = (peak pixel above background) / sky_rms.

    Reflects transparency directly: dimmer stars or brighter sky → lower SNR.
    """
    if sources is None or len(sources) == 0 or sky_rms <= 0:
        return float("nan")
    peaks = np.asarray(sources["peak"], dtype=float)
    return float(np.median(peaks / sky_rms))


# ---------------------------------------------------------------------------
# Trail detection
# ---------------------------------------------------------------------------

def detect_trails(
    data: np.ndarray,
    bkg_array: np.ndarray,
    sky_rms: float,
) -> int:
    """
    Detect linear artifacts — satellite trails, aircraft trails, meteors.

    Uses photutils segmentation at a low detection threshold, then flags
    sources whose elongation (semi-major / semi-minor axis ratio) exceeds 8
    and whose pixel area exceeds 500 px².

    Returns the count of detected trail-like features.

    Note: in very crowded fields, blended star chains can trigger a false
    positive.  Cross-check against the star_count if trail_count seems odd.
    """
    if sky_rms <= 0:
        return 0

    try:
        from photutils.segmentation import SourceCatalog, detect_sources
    except ImportError:
        return 0

    data_sub  = data - bkg_array
    threshold = 2.5 * sky_rms

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        seg = detect_sources(data_sub, threshold, npixels=200)

    if seg is None:
        return 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cat = SourceCatalog(data_sub, seg)

    try:
        elong_arr = np.asarray(cat.elongation, dtype=float)
        area_raw  = cat.area
        # photutils >= 1.x returns Quantity objects; unwrap .value if needed
        area_arr  = np.asarray(
            area_raw.value if hasattr(area_raw, "value") else area_raw,
            dtype=float,
        )
    except Exception:
        return 0

    trail_mask = (elong_arr > 8.0) & (area_arr > 500.0)
    return int(trail_mask.sum())


# ---------------------------------------------------------------------------
# Top-level frame analysis
# ---------------------------------------------------------------------------

def analyze_frame(path: str | Path) -> dict:
    """
    Full star-quality pipeline for a single FITS file.

    Returns a flat dict of all metrics.
    On failure returns only {'filename', 'path', 'error'}.
    """
    try:
        data, meta = load_fits(path)
        bkg_array, sky_median, sky_rms = estimate_background(data)
        sources    = detect_stars(data, bkg_array, sky_rms)
        star_count = len(sources) if sources is not None else 0

        ecc, elong, angle, consistency, center_ecc, edge_ecc, field_delta = \
            measure_star_morphology(data, bkg_array, sources, data.shape)

        sky_gradient = measure_sky_gradient(bkg_array)
        snr          = measure_snr(sources, sky_rms)
        trail_count  = detect_trails(data, bkg_array, sky_rms)

        return {
            **meta,
            "star_count":         star_count,
            "eccentricity":       ecc,
            "elongation_ratio":   elong,
            "trailing_angle_deg": angle,
            "angle_consistency":  consistency,
            "center_ecc":         center_ecc,
            "edge_ecc":           edge_ecc,
            "ecc_field_delta":    field_delta,
            "sky_adu":            sky_median,
            "sky_rms":            sky_rms,
            "sky_gradient":       sky_gradient,
            "snr":                snr,
            "trail_count":        trail_count,
        }

    except Exception as exc:
        return {
            "filename": Path(path).name,
            "path":     str(path),
            "error":    str(exc),
        }
