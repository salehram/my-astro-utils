"""
metrics.py — per-frame FITS quality metrics

Computes:
  - Sky background level and RMS (ADU)
  - Star count
  - FWHM (pixels) — median across detected stars via 2-D Gaussian fit
  - Eccentricity      — 0 = round (pure defocus), →1 = elongated (tracking/wind)
  - HFR (pixels)      — Half-Flux Radius, equivalent to PixInsight's HFR
  - SNR               — median peak-signal / sky-RMS across detected stars
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

import numpy as np
from astropy.io import fits
from astropy.modeling import fitting, models
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.detection import DAOStarFinder

# FWHM = 2 * sqrt(2 * ln2) * sigma  ≈  2.3548 * sigma
_FWHM_SIGMA: float = 2.0 * np.sqrt(2.0 * np.log(2.0))

# Half-size of the cutout used for Gaussian fitting / HFR (pixels either side of centroid)
_CUTOUT_HALF: int = 15

# Maximum stars kept for metric calculations (keeps runtime predictable)
_MAX_STARS: int = 150

# Stars used for HFR (subset of brightest; HFR calculation is more expensive)
_HFR_STARS: int = 50


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

    focallen = header.get("FOCALLEN")   # focal length in mm
    xpixsz   = header.get("XPIXSZ")    # pixel size in microns
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
        "plate_scale": plate_scale,  # arcsec/px, or None if FOCALLEN/XPIXSZ absent
    }
    return data, meta


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

def estimate_background(
    data: np.ndarray,
) -> tuple[np.ndarray, float, float]:
    """
    Sigma-clipped 2-D background model.

    Returns:
        background_array  — same shape as data
        sky_median (ADU)  — global median sky level
        sky_rms    (ADU)  — global sky noise (important for Bortle 9 monitoring)
    """
    # box_size: at least 32 px, at most 1/10 of the smaller image dimension
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

    Returns a photutils source table or None if no stars detected.
    The table is sorted by peak brightness (brightest first) and
    capped at _MAX_STARS entries.
    """
    data_sub = data - bkg_array
    daofind = DAOStarFinder(
        fwhm=4.0,                        # conservative initial FWHM estimate
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
    """
    Extract a square cutout centred on (x, y).

    Returns (cutout, x_offset, y_offset).
    Returns (None, 0, 0) if the star is too close to the image edge.
    """
    xi, yi = int(round(x)), int(round(y))
    r = _CUTOUT_HALF
    y0 = max(0, yi - r)
    y1 = min(data_sub.shape[0], yi + r + 1)
    x0 = max(0, xi - r)
    x1 = min(data_sub.shape[1], xi + r + 1)
    cut = data_sub[y0:y1, x0:x1]
    if cut.shape[0] < 5 or cut.shape[1] < 5:
        return None, 0, 0
    return cut, x0, y0


# ---------------------------------------------------------------------------
# FWHM and eccentricity
# ---------------------------------------------------------------------------

def measure_fwhm_eccentricity(
    data: np.ndarray,
    bkg_array: np.ndarray,
    sources,
) -> tuple[float, float]:
    """
    Fit a 2-D Gaussian to each star cutout.

    Returns:
        median_fwhm_px   — average of (FWHM_x + FWHM_y) / 2 across all stars
        median_ecc       — eccentricity [0, 1]; near 0 → round (defocus),
                           near 1 → elongated (tracking/wind problem)
    """
    if sources is None or len(sources) == 0:
        return float("nan"), float("nan")

    data_sub = data - bkg_array
    fitter = fitting.LevMarLSQFitter()
    fwhms: list[float] = []
    eccs:  list[float] = []

    for row in sources:
        cut, _x0, _y0 = _cutout(data_sub, row["x_centroid"], row["y_centroid"])
        if cut is None:
            continue

        yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
        cy, cx = cut.shape[0] / 2.0, cut.shape[1] / 2.0
        amp = float(np.clip(cut.max(), 1.0, None))

        init = models.Gaussian2D(
            amplitude=amp,
            x_mean=cx,   y_mean=cy,
            x_stddev=2.0, y_stddev=2.0,
            bounds={
                "x_stddev": (0.1, _CUTOUT_HALF),
                "y_stddev": (0.1, _CUTOUT_HALF),
                "amplitude": (0.0, None),
            },
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fitted = fitter(init, xx, yy, cut)

            sx = abs(fitted.x_stddev.value)
            sy = abs(fitted.y_stddev.value)
            if sx <= 0 or sy <= 0:
                continue

            fwhm_x = _FWHM_SIGMA * sx
            fwhm_y = _FWHM_SIGMA * sy
            fwhms.append((fwhm_x + fwhm_y) / 2.0)

            a = max(sx, sy)
            b = min(sx, sy)
            eccs.append(float(np.sqrt(max(0.0, 1.0 - (b / a) ** 2))))
        except Exception:
            continue

    if not fwhms:
        return float("nan"), float("nan")
    return float(np.median(fwhms)), float(np.median(eccs))


# ---------------------------------------------------------------------------
# HFR  (Half-Flux Radius)
# ---------------------------------------------------------------------------

def measure_hfr(
    data: np.ndarray,
    bkg_array: np.ndarray,
    sources,
) -> float:
    """
    Compute median Half-Flux Radius (pixels) — PixInsight's HFR equivalent.

    For each star the flux is accumulated in growing concentric rings.
    The radius at which cumulative flux equals 50% of total flux is the HFR.
    Works on per-star cutouts for efficiency (no full-image aperture calls).
    """
    if sources is None or len(sources) == 0:
        return float("nan")

    data_sub = data - bkg_array

    # Use brightest N stars only — HFR calc is more CPU-intensive
    n = min(_HFR_STARS, len(sources))
    peak_col = np.asarray(sources["peak"])
    bright_idx = np.argsort(peak_col)[::-1][:n]

    hfrs: list[float] = []
    for i in bright_idx:
        row = sources[i]
        xi = int(round(float(row["x_centroid"])))
        yi = int(round(float(row["y_centroid"])))
        r  = _CUTOUT_HALF
        y0 = max(0, yi - r);  y1 = min(data_sub.shape[0], yi + r + 1)
        x0 = max(0, xi - r);  x1 = min(data_sub.shape[1], xi + r + 1)
        cut = data_sub[y0:y1, x0:x1]
        if cut.shape[0] < 5 or cut.shape[1] < 5:
            continue

        # Distance of every pixel in the cutout from the star centre
        cy = float(yi - y0)
        cx = float(xi - x0)
        yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2).ravel()

        # Only count positive-flux pixels (avoids noise below sky)
        flux = cut.ravel()
        mask = flux > 0
        if mask.sum() < 5:
            continue

        order      = np.argsort(dist[mask])
        cum_flux   = np.cumsum(flux[mask][order])
        total_flux = cum_flux[-1]
        if total_flux <= 0:
            continue

        half_flux = total_flux / 2.0
        idx_half  = np.searchsorted(cum_flux, half_flux)
        if idx_half >= len(order):
            idx_half = len(order) - 1
        hfrs.append(float(dist[mask][order[idx_half]]))

    return float(np.median(hfrs)) if hfrs else float("nan")


# ---------------------------------------------------------------------------
# SNR
# ---------------------------------------------------------------------------

def measure_snr(sources, sky_rms: float) -> float:
    """
    Median per-star SNR: (peak pixel above background) / sky_rms.

    This is a simple but reliable proxy — it directly reflects how well
    each star rises above the sky noise.
    """
    if sources is None or len(sources) == 0 or sky_rms <= 0:
        return float("nan")
    peaks = np.asarray(sources["peak"], dtype=float)
    return float(np.median(peaks / sky_rms))


# ---------------------------------------------------------------------------
# Top-level frame analysis
# ---------------------------------------------------------------------------

def analyze_frame(path: str | Path) -> dict:
    """
    Full pipeline for a single FITS file.

    Returns a flat dict of all metrics.  On failure the dict contains
    only 'filename', 'path', and 'error'.
    """
    try:
        data, meta = load_fits(path)
        bkg_array, sky_median, sky_rms = estimate_background(data)
        sources = detect_stars(data, bkg_array, sky_rms)
        star_count = len(sources) if sources is not None else 0
        fwhm, ecc  = measure_fwhm_eccentricity(data, bkg_array, sources)
        hfr        = measure_hfr(data, bkg_array, sources)
        snr        = measure_snr(sources, sky_rms)
        plate_scale = meta.get("plate_scale")
        try:
            fwhm_arcsec = (
                float(fwhm) * plate_scale
                if plate_scale and not np.isnan(fwhm)
                else float("nan")
            )
        except (TypeError, ValueError):
            fwhm_arcsec = float("nan")

        return {
            **meta,
            "star_count":   star_count,
            "fwhm_px":      fwhm,
            "fwhm_arcsec":  fwhm_arcsec,
            "eccentricity": ecc,
            "hfr_px":       hfr,
            "sky_adu":      sky_median,
            "sky_rms":      sky_rms,
            "snr":          snr,
            "error":        None,
        }
    except Exception as exc:
        return {
            "filename": Path(path).name,
            "path":     str(path),
            "error":    str(exc),
        }
