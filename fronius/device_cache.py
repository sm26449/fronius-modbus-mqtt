"""Device cache for storing discovered device information"""

import json
import os
import time
from typing import Dict, List, Optional
from pathlib import Path
from .logging_setup import get_logger


class DeviceCache:
    """
    Persistent cache for device identification data.

    Stores discovered device info to avoid re-scanning on each startup.
    Supports automatic cache invalidation based on rescan_interval.
    """

    def __init__(self, cache_path: str = None):
        """
        Initialize device cache.

        Args:
            cache_path: Path to cache file (default: data/device_cache.json)
        """
        self.cache_path = cache_path or self._default_cache_path()
        self.devices: Dict[str, Dict] = {}
        self.discovered_at: float = 0
        self.log = get_logger()
        self._load_cache()

    def _default_cache_path(self) -> str:
        """Get default cache path relative to package"""
        base_dir = Path(__file__).parent.parent
        return str(base_dir / "data" / "device_cache.json")

    def _load_cache(self):
        """Load cache from disk"""
        try:
            if os.path.exists(self.cache_path):
                with open(self.cache_path, 'r') as f:
                    cache_data = json.load(f)
                    self.devices = cache_data.get('devices', {})
                    self.discovered_at = cache_data.get('discovered_at', 0)
                    device_count = len(self.devices)
                    if device_count > 0:
                        self.log.info(f"Loaded {device_count} cached device(s)")
        except json.JSONDecodeError as e:
            self.log.warning(f"Invalid cache file, will re-discover: {e}")
            self.devices = {}
            self.discovered_at = 0
        except Exception as e:
            self.log.warning(f"Could not load device cache: {e}")
            self.devices = {}
            self.discovered_at = 0

    def _save_cache(self):
        """Save cache to disk"""
        try:
            cache_dir = os.path.dirname(self.cache_path)
            if cache_dir and not os.path.exists(cache_dir):
                os.makedirs(cache_dir, exist_ok=True)

            with open(self.cache_path, 'w') as f:
                json.dump({
                    'devices': self.devices,
                    'discovered_at': self.discovered_at,
                    'updated_at': time.time()
                }, f, indent=2)
        except Exception as e:
            self.log.warning(f"Could not save device cache: {e}")

    def _make_key(self, device_id: int, device_type: str) -> str:
        """Generate cache key from device ID and type"""
        return f"{device_type}_{device_id}"

    def is_cache_valid(self, rescan_interval: int) -> bool:
        """
        Check if cache is still valid.

        Args:
            rescan_interval: Seconds before cache expires (0 = never expires)

        Returns:
            True if cache is valid and has devices
        """
        if not self.devices:
            return False

        if rescan_interval == 0:
            return True

        age = time.time() - self.discovered_at
        return age < rescan_interval

    def get_device(self, device_id: int, device_type: str) -> Optional[Dict]:
        """
        Get cached device info.

        Args:
            device_id: Modbus device ID
            device_type: 'inverter' or 'meter'

        Returns:
            Device info dict or None if not cached
        """
        key = self._make_key(device_id, device_type)
        return self.devices.get(key)

    def set_device(self, device_id: int, device_type: str, info: Dict):
        """
        Cache device info.

        Args:
            device_id: Modbus device ID
            device_type: 'inverter' or 'meter'
            info: Device information dictionary
        """
        key = self._make_key(device_id, device_type)
        info['cached_at'] = time.time()
        self.devices[key] = info
        self._save_cache()
        self.log.debug(f"Cached {device_type} ID {device_id}")

    def get_all_devices(self, device_type: str = None) -> List[Dict]:
        """
        Get all cached devices, optionally filtered by type.

        Args:
            device_type: Optional filter ('inverter' or 'meter')

        Returns:
            List of device info dictionaries
        """
        devices = []
        for key, device in self.devices.items():
            if device_type is None or device.get('device_type') == device_type:
                devices.append(device)
        return devices

    def get_inverters(self) -> List[Dict]:
        """Get all cached inverters"""
        return self.get_all_devices('inverter')

    def get_meters(self) -> List[Dict]:
        """Get all cached meters"""
        return self.get_all_devices('meter')

    def set_discovery_complete(self):
        """Mark discovery as complete and update timestamp"""
        self.discovered_at = time.time()
        self._save_cache()

    def clear(self):
        """Clear all cached data"""
        self.devices = {}
        self.discovered_at = 0
        self._save_cache()
        self.log.info("Device cache cleared")

    def invalidate(self, device_id: int, device_type: str):
        """
        Remove specific device from cache.

        Args:
            device_id: Modbus device ID
            device_type: 'inverter' or 'meter'
        """
        key = self._make_key(device_id, device_type)
        if key in self.devices:
            del self.devices[key]
            self._save_cache()
            self.log.debug(f"Invalidated cache for {device_type} ID {device_id}")

    def __len__(self) -> int:
        """Return number of cached devices"""
        return len(self.devices)
