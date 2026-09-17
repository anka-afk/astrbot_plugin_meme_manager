"""Storage adapters available to the image synchronization client."""

from .cloudflare_r2_provider import CloudflareR2Provider
from .lsky_provider import LskyProvider
from .provider_template import ProviderTemplate as ImageHostProvider
from .stardots_provider import StarDotsProvider
from .webdav_provider import WebDAVProvider

__all__ = [
    "LskyProvider",
    "StarDotsProvider",
    "CloudflareR2Provider",
    "WebDAVProvider",
    "ImageHostProvider",
]
