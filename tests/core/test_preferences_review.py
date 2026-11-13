import pytest

from lbr_dashboard.core.preferences import Preferences, PreferencesError, PreferencesStore


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"x":"\\udcff"}',
        b'{"schema_version":1,"x":{"\\udcff":1}}',
        b'{"schema_version":1,"recent_sources":[{"kind":"csv","location":"\\udcff"}]}',
    ],
)
def test_unencodable_preferences_recover_and_can_save_again(tmp_path, raw):
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    store.save(Preferences("dark"))
    store.save(Preferences("light"))
    path.write_bytes(raw)
    result = store.reload()
    assert result.origin == "backup" and result.warning
    store.save(Preferences("dark"))
    assert store.reload().preferences.theme == "dark"


def test_unencodable_extension_is_invalid_before_persistence():
    with pytest.raises(PreferencesError):
        Preferences(extra={"bad": "\udcff"})
