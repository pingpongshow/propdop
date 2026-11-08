"""WGS84 geodesy, only as much as the join needs.

Altitude datum is the trap here. ADS-B geometric altitude is height above the WGS84 ellipsoid,
while a surveyed site elevation is normally above mean sea level, and the two differ by the
geoid height -- tens of metres in most of the world. Getting it wrong moves the predicted slant
range by that amount, and 50 m of altitude error is about 30 m of position error. So the
receiver takes an explicit `geoid_offset_m` rather than quietly assuming they are the same.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)


def geodetic_to_ecef(
    lat_deg: ArrayLike, lon_deg: ArrayLike, alt_m: ArrayLike
) -> NDArray[np.float64]:
    """Geodetic (lat, lon, height above ellipsoid) to Earth-centred, Earth-fixed metres."""
    lat = np.radians(np.asarray(lat_deg, dtype=float))
    lon = np.radians(np.asarray(lon_deg, dtype=float))
    h = np.asarray(alt_m, dtype=float)
    sin_lat, cos_lat = np.sin(lat), np.cos(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat**2)
    x = (n + h) * cos_lat * np.cos(lon)
    y = (n + h) * cos_lat * np.sin(lon)
    z = (n * (1.0 - WGS84_E2) + h) * sin_lat
    return np.stack([x, y, z], axis=-1)


def enu_basis(lat_deg: float, lon_deg: float) -> NDArray[np.float64]:
    """Rows are the east, north and up unit vectors at a point, in ECEF."""
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    sl, cl = np.sin(lat), np.cos(lat)
    so, co = np.sin(lon), np.cos(lon)
    return np.array(
        [[-so, co, 0.0], [-sl * co, -sl * so, cl], [cl * co, cl * so, sl]], dtype=float
    )


def ecef_to_enu(
    points: ArrayLike, origin_lat: float, origin_lon: float, origin_ecef: ArrayLike
) -> NDArray[np.float64]:
    """ECEF points to local east/north/up metres about an origin."""
    d = np.asarray(points, dtype=float) - np.asarray(origin_ecef, dtype=float)
    return d @ enu_basis(origin_lat, origin_lon).T


@dataclass(frozen=True)
class Receiver:
    """A surveyed microphone position.

    `elevation_msl_m` is the site elevation above mean sea level; `geoid_offset_m` is the geoid
    height there (EGM96/EGM2008), so that ellipsoidal height = elevation + offset. Defaulting
    the offset to zero would be a silent tens-of-metres error, so it must be given explicitly
    for any real site.
    """

    lat: float
    lon: float
    elevation_msl_m: float
    geoid_offset_m: float = 0.0
    name: str = "node"

    @property
    def altitude_hae_m(self) -> float:
        """Height above the WGS84 ellipsoid -- the datum ADS-B geometric altitude uses."""
        return self.elevation_msl_m + self.geoid_offset_m

    @property
    def ecef(self) -> NDArray[np.float64]:
        return geodetic_to_ecef(self.lat, self.lon, self.altitude_hae_m)

    def enu_of(self, lat_deg, lon_deg, alt_hae_m) -> NDArray[np.float64]:
        return ecef_to_enu(
            geodetic_to_ecef(lat_deg, lon_deg, alt_hae_m), self.lat, self.lon, self.ecef
        )

    def slant_range_m(self, lat_deg, lon_deg, alt_hae_m) -> NDArray[np.float64]:
        return np.linalg.norm(self.enu_of(lat_deg, lon_deg, alt_hae_m), axis=-1)


def ecef_to_geodetic(points: ArrayLike) -> tuple[NDArray, NDArray, NDArray]:
    """ECEF metres to (lat deg, lon deg, height above ellipsoid). Iterative; converges fast."""
    p = np.atleast_2d(np.asarray(points, dtype=float))
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    lon = np.arctan2(y, x)
    r = np.hypot(x, y)
    lat = np.arctan2(z, r * (1.0 - WGS84_E2))
    for _ in range(6):
        sin_lat = np.sin(lat)
        n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat**2)
        h = r / np.cos(lat) - n
        lat = np.arctan2(z, r * (1.0 - WGS84_E2 * n / (n + h)))
    sin_lat = np.sin(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat**2)
    h = r / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def enu_to_geodetic(
    enu: ArrayLike, origin_lat: float, origin_lon: float, origin_ecef: ArrayLike
) -> tuple[NDArray, NDArray, NDArray]:
    """Local east/north/up metres back to geodetic coordinates."""
    ecef = np.asarray(origin_ecef, dtype=float) + np.atleast_2d(
        np.asarray(enu, dtype=float)
    ) @ enu_basis(origin_lat, origin_lon)
    return ecef_to_geodetic(ecef)
