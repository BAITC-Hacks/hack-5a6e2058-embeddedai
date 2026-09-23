import base64
import copy
import hashlib
import json
from html.parser import HTMLParser

import pytest

from money_graph.cli import main
from money_graph.demo import create_demo
from money_graph.offline_report import render_report
from money_graph.pipeline import EXPORTS, analyze, write_result


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    source = tmp_path_factory.mktemp("offline-input")
    create_demo(source)
    return analyze(source)


def test_report_contains_all_exact_identifiers_and_working_node_anchors(result):
    document = Document(render_report(result))
    rows = [attrs for tag, attrs in document.elements if tag == "tr" and "data-rank" in attrs]
    assert len(rows) == result["report"]["n_nodes"]
    assert [int(row["data-rank"]) for row in rows] == list(range(1, len(rows) + 1))
    anchors = {attrs["id"] for _, attrs in document.elements if "id" in attrs}
    links = [attrs["href"] for tag, attrs in document.elements if tag == "a"]
    assert all(link[1:] in anchors for link in links if link.startswith("#"))
    assert max(int(node["gid"]) for node in result["nodes"]) > 2**53
    for node in result["nodes"]:
        assert node["gid"] in document.text
        assert node["evidence"] in document.text
    text = " ".join(document.text)
    assert "Не seed и не граница" in text
    assert "не является вероятностью преступления" in text
    assert "не проверка истинных ролей" in text
    for digest in result["report"]["input_sha256"].values():
        assert digest in text


def test_report_has_no_remote_dependencies_and_hashes_only_its_own_script_and_css(result):
    source = render_report(result)
    document = Document(source)
    assert [tag for tag, _ in document.elements if tag == "script"] == ["script"]
    assert not any(
        tag in {"iframe", "img", "link", "object", "form"} for tag, _ in document.elements
    )
    for tag, attrs in document.elements:
        assert "src" not in attrs
        assert not any(key.startswith("on") for key in attrs)
        if tag == "a":
            assert attrs["href"].startswith("#") or attrs["href"] in {*EXPORTS, "run_report.json"}
    csp = next(
        attrs["content"]
        for tag, attrs in document.elements
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
    )
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    for tag in ("style", "script"):
        content = source.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
        assert f"{tag}-src 'sha256-{digest}'" in csp


def test_untrusted_text_cannot_create_elements_attributes_or_script(result):
    malicious = '<img src=x onerror=alert(1)><script>alert("owned")</script>&"\' '
    changed = copy.deepcopy(result)
    changed["report"]["rules_version"] = malicious
    changed["report"]["rules"]["version"] = malicious
    changed["report"]["warnings"].append(malicious)
    changed["report"]["input_sha256"][malicious] = malicious
    changed["top"][0]["why"] = malicious
    changed["top"][0]["gid"] = malicious
    changed["nodes"][0]["gid"] = malicious
    changed["nodes"][0]["evidence"] = malicious
    changed["nodes"][0]["warnings"] = [malicious]
    changed["clusters"][0]["hypothesis"] = malicious
    source = render_report(changed)
    document = Document(source)
    assert malicious in "".join(document.text)
    assert "<img" not in source
    assert sum(tag == "script" for tag, _ in document.elements) == 1
    assert not any(key.startswith("on") for _, attrs in document.elements for key in attrs)
    assert "alert(" not in source.split("<script>", 1)[1]


def test_report_is_deterministic_and_does_not_mutate_or_reanalyze_result(result, monkeypatch):
    original = copy.deepcopy(result)

    def forbidden(*args, **kwargs):
        raise AssertionError("HTML must reuse the calculated result")

    monkeypatch.setattr("money_graph.pipeline.analyze", forbidden)
    assert render_report(result) == render_report(original)
    assert result == original


def test_synthetic_public_run_is_clearly_labeled_in_downloaded_report(result):
    synthetic = copy.deepcopy(result)
    synthetic["report"]["synthetic"] = True
    assert "Синтетическая демонстрация: вымышленные узлы и переводы" in render_report(synthetic)
    assert "Синтетическая демонстрация" not in render_report(result)


def test_html_failure_preserves_previous_complete_generation(result, tmp_path, monkeypatch):
    output = tmp_path / "output"
    write_result(result, output)
    previous = {path.name: path.read_bytes() for path in output.iterdir()}

    def broken_report(_result):
        raise OSError("Report serialization failed")

    monkeypatch.setattr("money_graph.pipeline.render_report", broken_report)
    with pytest.raises(OSError, match="Report serialization failed"):
        write_result(result, output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ["output"]


def test_one_command_publishes_html_hash_and_can_replace_own_previous_output(
    result, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "Результат с пробелами"
    monkeypatch.setattr("money_graph.cli.analyze", lambda *args: result)
    for _ in range(2):
        main(["analyze", "--data", str(tmp_path / "source"), "--out", str(output)])
        response = json.loads(capsys.readouterr().out)
        assert response["status"] == "success"
        assert set(response["exports"]) == set(EXPORTS)
        assert response["report_html"]["path"] == str((output / "report.html").resolve())
        assert (
            response["report_html"]["sha256"]
            == hashlib.sha256((output / "report.html").read_bytes()).hexdigest()
        )
        assert "Граф денег" in (output / "report.html").read_text(encoding="utf-8")


def test_report_includes_every_row_at_supported_ten_thousand_node_limit(result):
    expanded = copy.deepcopy(result)
    template = expanded["nodes"][0]
    expanded["nodes"] = [
        template | {"gid": str(9_200_000_000_000_000_000 + index), "rank": index + 1}
        for index in range(10_000)
    ]
    expanded["report"]["n_nodes"] = 10_000
    source = render_report(expanded)
    document = Document(source)
    rows = [attrs for tag, attrs in document.elements if tag == "tr" and "data-rank" in attrs]
    assert len(rows) == 10_000
    assert len({row["id"] for row in rows}) == 10_000
    assert expanded["nodes"][-1]["gid"] in document.text
