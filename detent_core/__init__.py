"""Detent core. Version lives here so a stale import is visible.

Fusion keeps sys.modules across an add-in Stop/Run, so this can be an older
version than Detent.py even after a clean install. The command dialog shows
both; if they disagree, the package did not reload.

Named detent_core rather than core because every Fusion add-in shares one
interpreter and one sys.path - a package called "core" is a collision waiting
for the first other add-in that ships one.
"""

VERSION = "0.4.0"
