# manager/app/__version__.py
"""
Version information for Runner Manager.

This module defines the version of the Runner Manager application.
The version follows semantic versioning: MAJOR.MINOR.PATCH

- MAJOR: Incompatible API changes
- MINOR: Add functionality in a backward compatible manner
- PATCH: Backward compatible bug fixes
"""

__version__ = "0.9.0"
__version_info__ = tuple(int(x) for x in __version__.split("."))

# Additional version metadata
__author__ = "Loïc Bonavent"
__email__ = "loic.bonavent@umontpellier.fr"
__license__ = "Licence LGPL 3.0"
__description__ = "Runner Manager - A distributed task runner management system"
