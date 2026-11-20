import os
import shutil

import pytest

DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture(autouse=True)
def _private_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/cif_explorer in tests."""
    monkeypatch.setenv("CIF_EXPLORER_CONFIG", str(tmp_path / "config"))


@pytest.fixture(scope="session")
def mini_dic():
    from cif_explorer.dictionary import DictInfo
    return DictInfo([os.path.join(DATA, "mini.dic")])


@pytest.fixture
def mini_cif(tmp_path):
    """A writable copy of tests/data/mini.cif."""
    dest = tmp_path / "mini.cif"
    shutil.copy(os.path.join(DATA, "mini.cif"), dest)
    return str(dest)


def edit(path, old, new, count=1):
    with open(path) as fh:
        s = fh.read()
    assert old in s, old
    s = s.replace(old, new, count)
    # make sure the mtime changes even on coarse file systems
    st = os.stat(path)
    with open(path, "w") as fh:
        fh.write(s)
    os.utime(path, (st.st_atime, st.st_mtime + 2))
