"""Backward-compatible re-export.

The canonical import is `codatiel_i2.delivery.smtp2go`, which ships inside
the package and works in non-editable installs. This module stays so frozen
experiment scripts keep importing the original path.
"""

from codatiel_i2.delivery.smtp2go import *  # noqa: F401,F403
from codatiel_i2.delivery import smtp2go as _smtp2go

send_email = _smtp2go.send_email
load_key = _smtp2go.load_key
API_URL = _smtp2go.API_URL
