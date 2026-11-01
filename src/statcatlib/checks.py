"""Seal checks: what `WorkingCopy.seal` runs before it writes the manifest.

A seal check is a function of the working copy's view that returns what it
finds wrong, as `sdmxlib.edition.Finding` values. Pass checks to
``wc.seal(checks=[...])``: any finding refuses the seal, raising
`SealCheckError`, and leaves the working copy open to fix. ``seal`` runs none
by default.

Example:
    from statcatlib import checks

    wc.seal(checks=[checks.conf_status()])
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING

from sdmxlib.edition import Finding
from sdmxlib.model.dataflow import Dataflow

if TYPE_CHECKING:
    from statcatlib.store.store import EditionView

__all__ = ["SealCheck", "conf_status"]

#: A seal check: the working copy's view to everything found wrong in it.
type SealCheck = Callable[[EditionView], Iterable[Finding]]

#: The `Finding.check` name `conf_status` reports under.
CONF_STATUS_CHECK = "conf_status"


def conf_status(attribute: str = "CONF_STATUS", public: Iterable[str] = ("F",)) -> SealCheck:
    """A check that refuses observations whose confidentiality status is not public.

    For each dataflow whose data structure has the attribute and whose
    observations the edition holds, it reads them through
    ``view.registry.obs(flow)`` and reports one finding per observation whose
    *attribute* value is not in *public*. An observation with no value for the
    attribute is not reported.

    Args:
        attribute: The attribute holding the confidentiality status, by
            component id.
        public: The values that may be published: ``F`` (free) by default.

    Returns:
        The check, for ``wc.seal(checks=[...])``.
    """
    allowed = tuple(public)

    def check(view: EditionView) -> Iterator[Finding]:
        reg = view.registry
        for flow in reg.get_all(Dataflow):
            dsd = reg.dsd(flow)
            if dsd is None or not any(a.id == attribute for a in dsd.attributes):
                continue
            if reg.binding_for(flow) is None:
                continue
            obs = reg.obs(flow)
            column = obs.col(attribute)
            refused = obs.filter(~column.is_null(), ~column.is_in(allowed)).frame()
            keys = [d.id for d in dsd.dimensions if d.id in refused.columns]
            if dsd.time_dimension is not None and dsd.time_dimension.id in refused.columns:
                keys.append(dsd.time_dimension.id)
            for row in refused.iter_rows(named=True):
                key = ".".join(str(row[k]) for k in keys)
                detail = f"{attribute} is {row[attribute]!r}, not one of {', '.join(allowed)}"
                yield Finding(CONF_STATUS_CHECK, f"{flow.urn} {key}", detail)

    return check
