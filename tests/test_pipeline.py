from tests.conftest import FIXTURE  # noqa: F401

from backend.kg.acquire import AcquisitionError, normalize_repo_url, validate_ref
from scripts.evaluate import evaluate
import pytest


def test_fixture_precision_recall():
    r = evaluate(FIXTURE, verbose=False)
    for name, v in r.items():
        if name.startswith("_"):
            continue
        assert v["precision"] >= 0.95, (name, v)
        assert v["recall"] >= 0.95, (name, v)
    assert not r["_forbidden_violations"]
    assert not r["_apis"]["missing"]


@pytest.mark.parametrize("url", ["http://github.com/a/b", "https://evil.com/a/b", "file:///etc/passwd",
                                 "https://user:pw@github.com/a/b", "https://github.com/a", "https://github.com/-x/y",
                                 "ext::sh -c touch% /tmp/pwned"])
def test_unsafe_urls_rejected(url):
    with pytest.raises(AcquisitionError):
        normalize_repo_url(url)


def test_url_normalisation():
    assert normalize_repo_url("github.com/spring-projects/spring-petclinic")[1] == "spring-projects/spring-petclinic"
    assert normalize_repo_url("https://github.com/a/b.git")[0] == "https://github.com/a/b.git"
    assert normalize_repo_url("git@github.com:a/b.git")[1] == "a/b"


@pytest.mark.parametrize("ref", ["--upload-pack=x", "a..b", "x y", "$(id)"])
def test_bad_refs(ref):
    with pytest.raises(AcquisitionError):
        validate_ref(ref)
