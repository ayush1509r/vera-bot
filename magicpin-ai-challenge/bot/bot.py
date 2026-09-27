from __future__ import annotations
from composer import compose as _compose


def compose(category, merchant, trigger, customer=None):
    return _compose(category, merchant, trigger, customer)
