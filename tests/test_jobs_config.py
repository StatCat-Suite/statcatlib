"""The jobs configuration (Phase 5 D7): decoding, its refusals and the retention inequalities."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from statcatlib.jobs import (
    DEFAULT_KEEP_ROOTS,
    DEFAULT_UPLOAD_GRACE,
    AccessPolicy,
    ExpiryPolicy,
    JobsConfigError,
    Principal,
    RetentionPolicy,
    format_duration,
    load_jobs_config,
    parse_duration,
)

pytestmark = pytest.mark.unit

_RETENTION = """\
[retention]
backup = "30d"
gc_window = "35d"
tombstone_window = "400d"
inventory_window = "36d"
retire_after = "90d"
pod_staleness = "5m"
"""

_FULL = f"""\
format_version = 1

{_RETENTION}keep_roots = 12
upload_grace = "36h"

[access]
storage_account = "/subscriptions/s/resourceGroups/g/providers/Microsoft.Storage/storageAccounts/acct"
container = "catalogs"
allowlist = [
  {{ object_id = "11111111-0000-0000-0000-000000000001", name = "release job" }},
  {{ object_id = "11111111-0000-0000-0000-000000000002", name = "pods" }},
]
sentinels = ["/subscriptions/s/providers/Microsoft.Authorization/roleAssignments/a1"]
scopes = ["/subscriptions/s"]

[expiry]
enabled = true
not_before = "2027-01-15T00:00:00Z"
namespace = "statcat"
deployments = ["app=statcat-hot", "app=statcat-search"]
port = 8000
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "jobs.toml"
    path.write_text(text)
    return path


def _policy(*, keep_roots: int = DEFAULT_KEEP_ROOTS, **changes: timedelta) -> RetentionPolicy:
    values: dict[str, timedelta] = {
        "backup": timedelta(days=30),
        "gc_window": timedelta(days=35),
        "tombstone_window": timedelta(days=400),
        "inventory_window": timedelta(days=36),
        "retire_after": timedelta(days=90),
        "pod_staleness": timedelta(minutes=5),
        "upload_grace": DEFAULT_UPLOAD_GRACE,
    }
    values.update(changes)
    return RetentionPolicy(
        backup=values["backup"],
        gc_window=values["gc_window"],
        tombstone_window=values["tombstone_window"],
        inventory_window=values["inventory_window"],
        retire_after=values["retire_after"],
        pod_staleness=values["pod_staleness"],
        keep_roots=keep_roots,
        upload_grace=values["upload_grace"],
    )


class TestDecoding:
    def test_the_design_example_decodes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _FULL)

        config = load_jobs_config(path)

        assert config.format_version == 1
        assert config.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert config.retention == _policy(keep_roots=12, upload_grace=timedelta(hours=36))
        assert config.access == AccessPolicy(
            storage_account="/subscriptions/s/resourceGroups/g/providers/Microsoft.Storage/storageAccounts/acct",
            container="catalogs",
            allowlist=(
                Principal(object_id="11111111-0000-0000-0000-000000000001", name="release job"),
                Principal(object_id="11111111-0000-0000-0000-000000000002", name="pods"),
            ),
            sentinels=("/subscriptions/s/providers/Microsoft.Authorization/roleAssignments/a1",),
            scopes=("/subscriptions/s",),
        )
        assert config.expiry == ExpiryPolicy(
            enabled=True,
            not_before=datetime(2027, 1, 15, tzinfo=UTC),
            namespace="statcat",
            deployments=("app=statcat-hot", "app=statcat-search"),
            port=8000,
        )

    def test_defaults_keep_roots_upload_grace_no_access_and_expiry_off(self, tmp_path: Path) -> None:
        config = load_jobs_config(_write(tmp_path, f"format_version = 1\n{_RETENTION}"))

        assert config.retention.keep_roots == DEFAULT_KEEP_ROOTS == 10
        assert config.retention.upload_grace == DEFAULT_UPLOAD_GRACE == timedelta(hours=24)
        assert config.access is None
        assert config.expiry == ExpiryPolicy(enabled=False)

    def test_a_disabled_expiry_needs_no_gates(self, tmp_path: Path) -> None:
        config = load_jobs_config(_write(tmp_path, f"format_version = 1\n{_RETENTION}\n[expiry]\nenabled = false\n"))
        assert config.expiry.enabled is False

    def test_a_toml_offset_date_time_is_an_instant(self, tmp_path: Path) -> None:
        text = _FULL.replace('not_before = "2027-01-15T00:00:00Z"', "not_before = 2027-01-15T00:00:00Z")
        assert load_jobs_config(_write(tmp_path, text)).expiry.not_before == datetime(2027, 1, 15, tzinfo=UTC)

    def test_sentinels_may_be_empty_and_scopes_absent(self, tmp_path: Path) -> None:
        text = _FULL.replace(
            'sentinels = ["/subscriptions/s/providers/Microsoft.Authorization/roleAssignments/a1"]', "sentinels = []"
        ).replace('scopes = ["/subscriptions/s"]\n', "")
        access = load_jobs_config(_write(tmp_path, text)).access
        assert access is not None
        assert (access.sentinels, access.scopes) == ((), ())


class TestDurations:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("30s", timedelta(seconds=30)),
            ("5m", timedelta(minutes=5)),
            ("24h", timedelta(hours=24)),
            ("35d", timedelta(days=35)),
            ("0d", timedelta(0)),
        ],
    )
    def test_each_unit(self, text: str, value: timedelta) -> None:
        assert parse_duration(text) == value

    @pytest.mark.parametrize("text", ["35", "d", "3.5d", "-1d", "1w", "1 d", "1D", "1h30m", ""])
    def test_a_bad_duration_is_refused(self, text: str) -> None:
        with pytest.raises(JobsConfigError, match="no duration"):
            parse_duration(text)

    def test_a_bad_duration_in_the_file_names_the_key_and_the_file(self, tmp_path: Path) -> None:
        path = _write(tmp_path, f"format_version = 1\n{_RETENTION}".replace('"35d"', '"35 days"'))
        with pytest.raises(JobsConfigError, match=r"jobs\.toml: retention\.gc_window: '35 days' is no duration"):
            load_jobs_config(path)

    def test_a_number_in_place_of_a_duration_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, f"format_version = 1\n{_RETENTION}".replace('"35d"', "35"))
        with pytest.raises(JobsConfigError, match=r"retention\.gc_window must be a duration string"):
            load_jobs_config(path)

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (timedelta(days=35), "35d"),
            (timedelta(hours=36), "36h"),
            (timedelta(minutes=5), "5m"),
            (timedelta(seconds=90), "90s"),
            (timedelta(0), "0d"),
        ],
    )
    def test_format_duration_writes_the_largest_whole_unit(self, value: timedelta, text: str) -> None:
        assert format_duration(value) == text


class TestRefusals:
    def test_an_unknown_top_level_key(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match=r"the top level has unknown key\(s\) retension"):
            load_jobs_config(_write(tmp_path, f'format_version = 1\nretension = "x"\n{_RETENTION}'))

    def test_an_unknown_key_in_retention(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match=r"\[retention\] has unknown key\(s\) gc_windw"):
            load_jobs_config(_write(tmp_path, f'format_version = 1\n{_RETENTION}gc_windw = "1d"\n'))

    def test_an_unknown_key_in_an_allowlist_entry(self, tmp_path: Path) -> None:
        text = _FULL.replace('name = "pods" }', 'name = "pods", role = "reader" }')
        with pytest.raises(JobsConfigError, match=r"access\.allowlist\[1\] has unknown key\(s\) role"):
            load_jobs_config(_write(tmp_path, text))

    def test_an_unknown_key_in_expiry(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match=r"\[expiry\] has unknown key\(s\) gate"):
            load_jobs_config(_write(tmp_path, _FULL.replace("port = 8000", "port = 8000\ngate = 1")))

    def test_a_missing_format_version(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match="format_version is missing"):
            load_jobs_config(_write(tmp_path, _RETENTION))

    def test_another_format_version(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match="format_version 2 is not one"):
            load_jobs_config(_write(tmp_path, f"format_version = 2\n{_RETENTION}"))

    def test_a_missing_retention_section(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match=r"\[retention\] is missing"):
            load_jobs_config(_write(tmp_path, "format_version = 1\n"))

    def test_a_missing_retention_key(self, tmp_path: Path) -> None:
        text = f"format_version = 1\n{_RETENTION}".replace('retire_after = "90d"\n', "")
        with pytest.raises(JobsConfigError, match=r"\[retention\] is missing retire_after"):
            load_jobs_config(_write(tmp_path, text))

    def test_a_missing_access_key(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match=r"\[access\] is missing container"):
            load_jobs_config(_write(tmp_path, _FULL.replace('container = "catalogs"\n', "")))

    def test_an_enabled_expiry_needs_its_gates(self, tmp_path: Path) -> None:
        text = _FULL.replace('not_before = "2027-01-15T00:00:00Z"\n', "")
        with pytest.raises(JobsConfigError, match=r"enabled = true is missing not_before"):
            load_jobs_config(_write(tmp_path, text))

    def test_an_instant_with_no_zone(self, tmp_path: Path) -> None:
        text = _FULL.replace('"2027-01-15T00:00:00Z"', '"2027-01-15T00:00:00"')
        with pytest.raises(JobsConfigError, match="has no time zone"):
            load_jobs_config(_write(tmp_path, text))

    def test_a_bad_port(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match="no TCP port"):
            load_jobs_config(_write(tmp_path, _FULL.replace("port = 8000", "port = 70000")))

    @pytest.mark.parametrize(
        ("old", "new", "match"),
        [
            ("keep_roots = 12", "keep_roots = true", "keep_roots must be an integer"),
            ("keep_roots = 12", 'keep_roots = "12"', "keep_roots must be an integer"),
            ('container = "catalogs"', "container = 1", "access.container must be a non-empty string"),
            ('container = "catalogs"', 'container = ""', "access.container must be a non-empty string"),
            ("enabled = true", 'enabled = "yes"', "expiry.enabled must be true or false"),
            ('scopes = ["/subscriptions/s"]', 'scopes = "/subscriptions/s"', "access.scopes must be an array"),
        ],
    )
    def test_a_value_of_the_wrong_type(self, tmp_path: Path, old: str, new: str, match: str) -> None:
        with pytest.raises(JobsConfigError, match=match):
            load_jobs_config(_write(tmp_path, _FULL.replace(old, new)))

    def test_a_file_that_is_not_toml(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match="is not TOML"):
            load_jobs_config(_write(tmp_path, "format_version = = 1\n"))

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(JobsConfigError, match="cannot read the jobs configuration"):
            load_jobs_config(tmp_path / "absent.toml")

    def test_jobs_config_error_is_a_value_error(self) -> None:
        assert issubclass(JobsConfigError, ValueError)


class TestInequalities:
    @pytest.mark.parametrize(
        ("changes", "match"),
        [
            ({"backup": timedelta(days=40)}, r"backup \(40d\) must not exceed gc_window \(35d\)"),
            (
                {"tombstone_window": timedelta(days=30), "backup": timedelta(days=20)},
                r"gc_window \(35d\) must not exceed tombstone_window \(30d\)",
            ),
            (
                {"inventory_window": timedelta(days=35, hours=23)},
                r"inventory_window \(863h\) must be at least gc_window \(35d\) plus 1d",
            ),
            (
                {"pod_staleness": timedelta(days=36)},
                r"pod_staleness \(36d\) must be below inventory_window \(36d\)",
            ),
            ({"keep_roots": 1}, r"keep_roots \(1\) must be at least 2"),
            ({"retire_after": timedelta(0)}, r"retire_after \(0d\) must be positive"),
            ({"upload_grace": timedelta(seconds=-1)}, r"upload_grace \(-1 day, 23:59:59\) must be positive"),
        ],
        ids=["backup", "tombstone", "inventory", "pod-staleness", "keep-roots", "positive", "negative"],
    )
    def test_each_refusal_names_both_values(self, changes: dict[str, timedelta | int], match: str) -> None:
        keep_roots = changes.pop("keep_roots", DEFAULT_KEEP_ROOTS)
        assert isinstance(keep_roots, int)
        durations = {key: value for key, value in changes.items() if isinstance(value, timedelta)}
        with pytest.raises(JobsConfigError, match=match):
            _policy(keep_roots=keep_roots, **durations).check()

    def test_the_boundaries_pass(self) -> None:
        _policy(
            backup=timedelta(days=35),
            tombstone_window=timedelta(days=35),
            inventory_window=timedelta(days=36),
            pod_staleness=timedelta(days=35, hours=23),
            keep_roots=2,
        ).check()

    def test_the_file_is_checked_on_load(self, tmp_path: Path) -> None:
        text = f"format_version = 1\n{_RETENTION}".replace('backup = "30d"', 'backup = "40d"')
        with pytest.raises(JobsConfigError, match=r"jobs\.toml: retention: backup \(40d\) must not exceed"):
            load_jobs_config(_write(tmp_path, text))

    def test_a_zero_duration_in_the_file_is_refused_as_not_positive(self, tmp_path: Path) -> None:
        text = f"format_version = 1\n{_RETENTION}".replace('pod_staleness = "5m"', 'pod_staleness = "0s"')
        with pytest.raises(JobsConfigError, match=r"pod_staleness \(0d\) must be positive"):
            load_jobs_config(_write(tmp_path, text))
