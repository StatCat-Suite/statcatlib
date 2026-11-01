"""Seal checks: `conf_status`, and a failing check leaving the working copy open (design G7)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import polars as pl
import pytest
import sdmxlib as sl
from sdmxlib import InternationalString, Ref
from sdmxlib.edition import Finding

import statcatlib as sc
from statcatlib import checks
from tests.conftest import concept_of

pytestmark = pytest.mark.unit


def _flow(wc: sc.WorkingCopy, statuses: list[str | None]) -> None:
    """A dataflow with a ``CONF_STATUS`` attribute, one observation per status."""
    geo = sl.Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=InternationalString(en="Geography"),
        codes=[sl.Code(id=f"G{i}", name=InternationalString(en=f"G{i}")) for i in range(len(statuses))],
    )
    conf = sl.Codelist(
        id="CL_CONF",
        maintainer="TEST",
        name=InternationalString(en="Confidentiality"),
        codes=[sl.Code(id=c, name=InternationalString(en=c)) for c in ("F", "C", "D")],
    )
    dsd = sl.DataStructure(
        id="DSD_CONF",
        maintainer="TEST",
        name=InternationalString(en="DSD"),
        dimensions=[sl.Dimension(id="GEO", concept=concept_of("GEO"), representation=geo)],
        attributes=[
            sl.Attribute(
                id="CONF_STATUS",
                concept=concept_of("CONF_STATUS"),
                representation=conf,
                attachment=sl.AttachmentLevel.OBSERVATION,
            )
        ],
    )
    flow = sl.Dataflow(id="DF_CONF", maintainer="TEST", name=InternationalString(en="Flow"), structure=dsd)
    reg = wc.registry
    reg.add_all([geo, conf, dsd, flow])
    frame = pl.DataFrame(
        {"GEO": [f"G{i}" for i in range(len(statuses))], "CONF_STATUS": statuses},
        schema={"GEO": pl.String, "CONF_STATUS": pl.String},
    )
    reg.write_observations(schema="obs", table="df_conf", source=frame)
    reg.add(sl.DataflowBinding.from_dsd(dsd, dataflow=Ref.to(flow), schema="obs", table="df_conf"))


class TestConfStatus:
    def test_public_observations_pass(self, wc: sc.WorkingCopy) -> None:
        _flow(wc, ["F", "F", None])
        assert list(checks.conf_status()(wc.view())) == []

    def test_each_confidential_observation_is_a_finding(self, wc: sc.WorkingCopy) -> None:
        _flow(wc, ["F", "C", "D"])
        findings = list(checks.conf_status()(wc.view()))
        assert sorted(f.subject.rsplit(" ", 1)[1] for f in findings) == ["G1", "G2"]
        assert all(f.check == "conf_status" for f in findings)
        assert "CONF_STATUS is 'C'" in {f.detail.split(",")[0] for f in findings}

    def test_public_values_are_configurable(self, wc: sc.WorkingCopy) -> None:
        _flow(wc, ["F", "C"])
        assert list(checks.conf_status(public=("F", "C"))(wc.view())) == []

    def test_a_flow_without_the_attribute_is_skipped(self, wc: sc.WorkingCopy) -> None:
        _flow(wc, ["C"])
        assert list(checks.conf_status(attribute="OBS_STATUS")(wc.view())) == []


class TestSealWithChecks:
    def test_a_finding_refuses_the_seal_and_leaves_the_working_copy_open(
        self, catalog: sc.Catalog, tmp_path: Path
    ) -> None:
        wc = catalog.product("p").working_copy()
        _flow(wc, ["C"])
        with pytest.raises(sc.SealCheckError, match="CONF_STATUS is 'C'") as caught:
            wc.seal(checks=[checks.conf_status()])
        assert len(caught.value.findings) == 1
        assert not (tmp_path / "catalog" / "products" / "p" / wc.edition_id / "manifest.json").exists()
        assert wc.view().registry is wc.registry
        edition = wc.seal(checks=[checks.conf_status(public=("F", "C"))])
        assert edition.edition_id == wc.edition_id

    def test_a_custom_check_sees_the_working_copys_view(self, wc: sc.WorkingCopy) -> None:
        seen: list[int] = []

        def count(view: sc.EditionView) -> Iterable[Finding]:
            seen.append(view.query(sc.BaseDataset).count())
            return []

        wc.put(
            sc.OpenDataset(identifier="d", title=InternationalString(en="D"), description=InternationalString(en="D"))
        )
        wc.seal(checks=[count])
        assert seen == [1]

    def test_seal_runs_no_check_by_default(self, wc: sc.WorkingCopy) -> None:
        _flow(wc, ["C"])
        assert wc.seal().edition_id == wc.edition_id
