"""Tests for turnstone.core.web_helpers — version_html() cache-busting."""

from __future__ import annotations

import os

import pytest


class TestVersionHtml:
    def test_app_css_gets_version(self):
        from turnstone.core.web_helpers import version_html

        html = '<link rel="stylesheet" href="/shared/base.css">'
        result = version_html(html)
        assert "?v=" in result
        assert "/shared/base.css?v=" in result

    def test_app_js_gets_version(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js"></script>'
        result = version_html(html)
        assert "/static/app.js?v=" in result

    def test_shared_js_gets_version(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/utils.js"></script>'
        result = version_html(html)
        assert "/shared/utils.js?v=" in result

    def test_vendored_katex_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<link rel="stylesheet" href="/shared/katex-0.18.4/katex.min.css">'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hljs_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hljs-11.11.1/highlight.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_mermaid_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/mermaid-11.16.1/mermaid.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendored_hls_skipped(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hls-1.6.17/hls.min.js"></script>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_vendor_prefix_lookalike_is_still_versioned(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/shared/hls-2-player.js"></script>'
        result = version_html(html)
        assert "/shared/hls-2-player.js?v=" in result

    def test_external_urls_not_modified(self):
        from turnstone.core.web_helpers import version_html

        html = (
            '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono" rel="stylesheet">'
        )
        result = version_html(html)
        assert result == html  # unchanged

    def test_docs_link_not_modified(self):
        from turnstone.core.web_helpers import version_html

        html = '<a href="/docs#/System:%20Settings" target="_blank">docs</a>'
        result = version_html(html)
        assert result == html  # unchanged

    def test_multiple_tags(self):
        from turnstone import __version__
        from turnstone.core.web_helpers import version_html

        html = (
            '<link rel="stylesheet" href="/shared/base.css">\n'
            '<link rel="stylesheet" href="/shared/katex-0.18.4/katex.min.css">\n'
            '<link rel="stylesheet" href="/static/style.css">\n'
            '<script src="/shared/utils.js"></script>\n'
            '<script src="/shared/hljs-11.11.1/highlight.min.js"></script>\n'
            '<script src="/static/app.js"></script>'
        )
        result = version_html(html)
        assert f'/shared/base.css?v={__version__}"' in result
        assert f'/static/style.css?v={__version__}"' in result
        assert f'/shared/utils.js?v={__version__}"' in result
        assert f'/static/app.js?v={__version__}"' in result
        # Vendored libs unchanged
        assert '/shared/katex-0.18.4/katex.min.css"' in result
        assert '/shared/hljs-11.11.1/highlight.min.js"' in result

    def test_version_matches_package(self):
        from turnstone import __version__
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js"></script>'
        result = version_html(html)
        assert f"?v={__version__}" in result

    def test_double_apply_is_idempotent(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js"></script>'
        once = version_html(html)
        twice = version_html(once)
        assert once == twice
        assert twice.count("?v=") == 1

    def test_existing_query_string_preserved(self):
        from turnstone.core.web_helpers import version_html

        html = '<script src="/static/app.js?foo=bar"></script>'
        result = version_html(html)
        assert result == html  # unchanged — already has query string


class TestRevalidatingStaticFiles:
    def test_same_versioned_url_revalidates_after_asset_changes(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        old_body = b"export const generation = 'old';"
        new_body = b"export const generation = 'new';"
        assert len(old_body) == len(new_body)
        asset.write_bytes(old_body)
        original_stat = asset.stat()
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            first = client.get(url)
            assert first.status_code == 200
            assert first.headers["cache-control"] == "no-cache"
            assert "last-modified" not in first.headers
            old_etag = first.headers["etag"]

            asset.write_bytes(new_body)
            os.utime(
                asset,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            changed_stat = asset.stat()
            assert changed_stat.st_size == original_stat.st_size
            assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns

            changed = client.get(url, headers={"If-None-Match": old_etag})
            assert changed.status_code == 200
            assert changed.content == new_body
            assert changed.headers["etag"] != old_etag
            assert changed.headers["cache-control"] == "no-cache"
            assert "last-modified" not in changed.headers

            stale_date_only = client.get(
                url,
                headers={"If-Modified-Since": "Wed, 31 Dec 9999 23:59:59 GMT"},
            )
            assert stale_date_only.status_code == 200
            assert stale_date_only.content == new_body

            unchanged = client.get(url, headers={"If-None-Match": changed.headers["etag"]})
            assert unchanged.status_code == 304
            assert unchanged.headers["cache-control"] == "no-cache"

    def test_content_change_is_detected_when_every_metadata_field_collides(
        self, tmp_path, monkeypatch
    ):
        """A rewritten asset must change its validator even when the
        filesystem metadata is byte-for-byte identical.

        The metadata signature the validator used to be keyed on
        (``mtime_ns``, ``ctime_ns``, ``size``) cannot tell the replacement
        from the asset it replaced when a deployment restores the timestamp.
        This pins the collision deterministically: the replacement is given
        the SAME ``mtime``, the SAME ``ctime`` and the SAME size, so a
        metadata-derived validator would report "unchanged" and answer a
        conditional request with 304 and stale bytes.

        The collision is forced by pinning ``os.stat`` for the mount, so the
        test does not depend on the filesystem's timestamp resolution.
        """
        import hashlib

        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        old_body = b"export const generation = 'old';"
        new_body = b"export const generation = 'new';"
        assert len(old_body) == len(new_body)
        asset.write_bytes(old_body)
        original_stat = asset.stat()

        # Pin the metadata the mount sees: after the rewrite the file has the
        # same size, the same mtime and the same ctime as the original.
        # A stat_result carries FLOAT timestamps (the nanosecond fields are
        # derived from them), so pin the three timestamps to one fixed value.
        fixed_times = (1_700_000_000.0, 1_700_000_000.0, 1_700_000_000.0)
        rewritten_stat = os.stat_result(tuple(original_stat)[:7] + fixed_times)
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            if os.fspath(path) == str(asset):
                return rewritten_stat
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", fake_stat)

        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            first = client.get(url)
            assert first.status_code == 200
            assert first.content == old_body
            old_etag = first.headers["etag"]

            asset.write_bytes(new_body)
            os.utime(
                asset,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            # The metadata the mount sees is identical before and after, so
            # only the CONTENT can decide whether the asset changed.
            assert rewritten_stat.st_size == original_stat.st_size
            assert rewritten_stat.st_mtime_ns == rewritten_stat.st_ctime_ns

            changed = client.get(url, headers={"If-None-Match": old_etag})
            assert changed.status_code == 200
            assert changed.content == new_body
            assert changed.headers["etag"] != old_etag
            assert changed.headers["etag"] == (f'"sha256-{hashlib.sha256(new_body).hexdigest()}"')

            # The current validator still revalidates to 304.
            unchanged = client.get(url, headers={"If-None-Match": changed.headers["etag"]})
            assert unchanged.status_code == 304

    def test_unchanged_asset_keeps_its_validator(self, tmp_path):
        """An asset that did not change keeps the validator it had."""
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        body = b"export const generation = 'same';"
        asset.write_bytes(body)
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            first = client.get(url)
            assert first.status_code == 200
            assert first.headers["etag"]

            second = client.get(url)
            assert second.status_code == 200
            assert second.headers["etag"] == first.headers["etag"]
            assert second.content == body

            revalidated = client.get(url, headers={"If-None-Match": first.headers["etag"]})
            assert revalidated.status_code == 304

    def test_validator_describes_the_bytes_the_response_carries(self, tmp_path, monkeypatch):
        """The validator and the body must come from one snapshot.

        The validator used to be computed from one read of the file while the
        body was streamed by a later read, so a rewrite between the two
        produced a response whose validator described bytes it did not carry.
        The rewrite is timed for the SECOND read of the asset, which is where
        the two implementations differ.
        """
        import hashlib

        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        old_body = b"export const generation = 'old';"
        new_body = b"export const generation = 'new';"
        assert len(old_body) == len(new_body)
        asset.write_bytes(old_body)
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        # Rewrite the asset as the mount opens it for the SECOND time.  A
        # single-snapshot implementation never reaches a second read, so the
        # rewrite lands on the same bytes the validator was taken from.
        real_open = open
        opens = {"count": 0}

        def rewriting_open(file, mode="r", *args, **kwargs):
            path = os.fspath(file)
            if "b" in mode and path == str(asset):
                opens["count"] += 1
                if opens["count"] == 2:
                    with real_open(path, "wb") as handle:
                        handle.write(new_body)
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", rewriting_open)

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            resp = client.get(url)

        assert resp.status_code == 200
        expected = '"sha256-' + hashlib.sha256(resp.content).hexdigest() + '"'
        assert resp.headers["etag"] == expected

    def test_rapid_same_size_replacements_always_revalidate(self, tmp_path):
        """Repeated same-size replacements never answer 304 with stale bytes.

        Every replacement preserves the mtime, so the validator can only be
        right if it is derived from the content.
        """
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = "/static/app.js"
            for generation in range(25):
                body = f"export const generation = '{generation:02d}';".encode()
                asset.write_bytes(body)
                stat = asset.stat()
                os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                resp = client.get(url)
                assert resp.status_code == 200
                assert resp.content == body
                assert resp.headers["etag"] == (
                    f'"sha256-{__import__("hashlib").sha256(body).hexdigest()}"'
                )

    def test_byte_range_is_served_from_the_same_snapshot(self, tmp_path):
        """A ranged response carries a slice of the bytes the validator covers."""
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        body = bytes(range(64))
        asset.write_bytes(body)
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            full = client.get(url)
            assert full.status_code == 200
            assert full.headers["accept-ranges"] == "bytes"

            ranged = client.get(url, headers={"Range": "bytes=8-15"})
            assert ranged.status_code == 206
            assert ranged.content == body[8:16]
            assert ranged.headers["content-range"] == f"bytes 8-15/{len(body)}"
            assert ranged.headers["etag"] == full.headers["etag"]

    def test_multiple_ranges_use_the_multipart_byteranges_layout(self, tmp_path):
        """A multi-range response keeps the byte layout browsers expect.

        The multipart body is part of the wire format, so it is pinned here
        rather than left to the file-response implementation it replaced.
        """
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone import __version__
        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        body = bytes(range(64))
        asset.write_bytes(body)
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            url = f"/static/app.js?v={__version__}"
            resp = client.get(url, headers={"Range": "bytes=0-3,8-11"})

        assert resp.status_code == 206
        content_type = resp.headers["content-type"]
        assert content_type.startswith("multipart/byteranges; boundary=")
        boundary = content_type.split("boundary=")[1]

        expected = (
            (
                f"--{boundary}\r\n"
                "Content-Type: text/javascript; charset=utf-8\r\n"
                "Content-Range: bytes 0-3/64\r\n"
                "\r\n"
            ).encode()
            + body[0:4]
            + b"\r\n"
            + (
                f"--{boundary}\r\n"
                "Content-Type: text/javascript; charset=utf-8\r\n"
                "Content-Range: bytes 8-11/64\r\n"
                "\r\n"
            ).encode()
            + body[8:12]
            + b"\r\n"
            + f"--{boundary}--".encode()
        )
        assert resp.content == expected
        assert resp.headers["content-length"] == str(len(expected))

    def test_the_range_helpers_this_relies_on_keep_their_behaviour(self):
        """Guard the file-response helpers the range path is built on.

        The range parsing and the multipart layout are taken from the file
        response, so the accepted syntax, the error split and the multipart
        bytes are pinned here.  A dependency bump that changed any of them
        would alter what browsers receive, and this test is where that
        shows up first.
        """
        from starlette.responses import FileResponse

        assert FileResponse._parse_range_header("bytes=0-5", 64) == [(0, 6)]
        assert FileResponse._parse_range_header("bytes=0-3,8-11", 64) == [(0, 4), (8, 12)]
        assert FileResponse._parse_range_header("bytes=8-", 64) == [(8, 64)]
        assert FileResponse._parse_range_header("bytes=-4", 64) == [(60, 64)]

        with pytest.raises(Exception) as malformed:
            FileResponse._parse_range_header("bytes=abc", 64)
        assert type(malformed.value).__name__ == "MalformedRangeHeader"

        with pytest.raises(Exception) as unsatisfiable:
            FileResponse._parse_range_header("bytes=999-", 64)
        assert type(unsatisfiable.value).__name__ == "RangeNotSatisfiable"
        assert unsatisfiable.value.max_size == 64

    def test_concurrent_reads_and_replacements_never_serve_a_mismatched_validator(self, tmp_path):
        """Under contention the validator always matches the bytes served.

        Readers and a writer run at the same time; every response must carry
        a validator that describes its own body.  The writer replaces the
        asset in place, so a reader can also observe a torn body - that is a
        property of a non-atomic write, not of the server, and the validator
        must describe whatever body was actually served.
        """
        import hashlib
        import threading

        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone.core.web_helpers import RevalidatingStaticFiles

        asset = tmp_path / "app.js"
        bodies = [
            f"export const generation = '{generation:02d}';".encode() for generation in range(8)
        ]
        assert len({len(body) for body in bodies}) == 1
        asset.write_bytes(bodies[0])
        app = Starlette(
            routes=[Mount("/static", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        errors: list[str] = []
        stop = threading.Event()

        def writer() -> None:
            index = 0
            while not stop.is_set():
                body = bodies[index % len(bodies)]
                index += 1
                asset.write_bytes(body)
                stat = asset.stat()
                os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        def reader() -> None:
            with TestClient(app) as client:
                while not stop.is_set():
                    resp = client.get("/static/app.js")
                    if resp.status_code != 200:
                        errors.append(f"status {resp.status_code}")
                        continue
                    expected = '"sha256-' + hashlib.sha256(resp.content).hexdigest() + '"'
                    if resp.headers["etag"] != expected:
                        errors.append(
                            f"validator {resp.headers['etag']} does not describe the body"
                        )

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)
        writer_thread.start()
        reader_thread.start()
        # Let the two run against each other for a fixed window, then stop.
        reader_thread.join(timeout=5)
        stop.set()
        for thread in (writer_thread, reader_thread):
            thread.join(timeout=10)
            assert not thread.is_alive(), "contention thread did not finish"

        assert errors == []

    def test_version_named_vendor_asset_is_immutable(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone.core.web_helpers import RevalidatingStaticFiles

        vendor_dir = tmp_path / "katex-0.18.4"
        vendor_dir.mkdir()
        (vendor_dir / "katex.min.css").write_text(".katex {}", encoding="utf-8")
        app = Starlette(
            routes=[Mount("/shared", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            resp = client.get("/shared/katex-0.18.4/katex.min.css")

        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert resp.headers["etag"]

    def test_missing_asset_is_not_cached(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient

        from turnstone.core.web_helpers import RevalidatingStaticFiles

        app = Starlette(
            routes=[Mount("/shared", app=RevalidatingStaticFiles(directory=str(tmp_path)))]
        )

        with TestClient(app) as client:
            resp = client.get("/shared/not-deployed-yet.js")

        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"


class TestStaticAssetCacheControl:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("interactive.js", "no-cache"),
            ("hls-2-player.js", "no-cache"),
            ("katex-0.18.4/katex.min.css", "public, max-age=31536000, immutable"),
            ("hljs-11.11.1/highlight.min.js", "public, max-age=31536000, immutable"),
            ("katex-0.18.4/../private.json", "no-store"),
            (r"katex-0.18.4\..\private.json", "no-store"),
            ("nested//asset.js", "no-store"),
        ],
    )
    def test_policy_requires_a_canonical_exact_vendor_path(self, path, expected):
        from turnstone.core.web_helpers import static_asset_cache_control

        assert static_asset_cache_control(path) == expected

    def test_every_packaged_versioned_vendor_directory_uses_the_shared_policy(self):
        import re
        from pathlib import Path

        import turnstone
        from turnstone.core.web_helpers import static_asset_cache_control, version_html

        shared_dir = Path(turnstone.__file__).resolve().parent / "shared_static"
        versioned_dir = re.compile(r"^[a-z][a-z0-9_-]*-\d+(?:\.\d+)+$")
        vendor_dirs = sorted(
            path.name
            for path in shared_dir.iterdir()
            if path.is_dir() and versioned_dir.fullmatch(path.name)
        )
        assert vendor_dirs

        for directory in vendor_dirs:
            asset_path = f"{directory}/asset.js"
            assert static_asset_cache_control(asset_path) == "public, max-age=31536000, immutable"
            html = f'<script src="/shared/{asset_path}"></script>'
            assert version_html(html) == html


class TestLatin1SafeFilename:
    """Content-Disposition filename sanitizer — must yield a value that is
    both latin-1 encodable (Starlette) and control-char free (h11)."""

    def _assert_wire_safe(self, out: str) -> None:
        # Independent oracle — deliberately does NOT reuse the impl's
        # isprintable() gate (that would pass by construction). Every char
        # must be printable ASCII (0x20..0x7e) and neither quoted-string
        # metacharacter, so the value is latin-1 clean, control-free, and
        # safely quotable.
        assert all(0x20 <= ord(c) <= 0x7E and c not in '"\\' for c in out)

    def test_plain_ascii_unchanged(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        assert latin1_safe_filename("report_2026.md") == "report_2026.md"

    def test_non_latin1_folds_to_question_marks(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # CJK + em dash (U+2014) are printable but non-latin-1 → fold to '?'.
        out = latin1_safe_filename("文書 — v1.md")
        assert out == "?? ? v1.md"
        self._assert_wire_safe(out)

    def test_latin1_but_control_chars_are_stripped(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # All latin-1 encodable, so the old strip/fold left them in the header
        # and the HTTP server layer then 500'd (h11 rejects NUL/CR/LF/FF/VT;
        # httptools is stricter). NUL / form-feed / DEL / TAB / VT / C1-NEL
        # (0x85) must all be dropped, not merely folded.
        out = latin1_safe_filename("a\x00b\x0cc\x7fd\te\x0bf\x85g.md")
        assert out == "abcdefg.md"
        self._assert_wire_safe(out)

    def test_crlf_and_quote_stripped(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        out = latin1_safe_filename('a"\r\nX-Evil: 1.md')
        assert "\r" not in out and "\n" not in out and '"' not in out
        self._assert_wire_safe(out)

    def test_backslash_stripped(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # Backslash is the RFC 6266 quoted-pair escape inside filename="..." —
        # a trailing '\' would escape the closing quote, and '\x' mid-name
        # becomes a spurious escape. Both must be dropped (Windows-origin
        # uploads legitimately carry '\').
        assert latin1_safe_filename("dir\\file.md") == "dirfile.md"
        assert latin1_safe_filename("trailing\\") == "trailing"
        self._assert_wire_safe(latin1_safe_filename("a\\b\\c"))

    def test_empty_after_sanitizing_uses_fallback(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # A name of only quotes / controls sanitizes to empty → fallback,
        # never ``filename=""``.
        assert latin1_safe_filename('"""') == "attachment"
        assert latin1_safe_filename("\x00\x0c\x7f") == "attachment"
        assert latin1_safe_filename("", fallback="preview") == "preview"

    def test_all_non_latin1_stays_non_empty_no_fallback(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # An all-CJK name folds to '???' (truthy) — must NOT hit the fallback.
        assert latin1_safe_filename("日本語", fallback="preview") == "???"

    def test_fallback_is_also_sanitized(self):
        from turnstone.core.web_helpers import latin1_safe_filename

        # The fallback fires only when the name sanitizes to empty, and it is
        # cleaned by the SAME rules — a caller can't reintroduce the crash /
        # corruption through an unsafe fallback.
        assert latin1_safe_filename("", fallback="—\x00.txt") == "?.txt"
        self._assert_wire_safe(latin1_safe_filename("", fallback="bad\\\x00name"))
        # If even the fallback sanitizes to empty, a safe constant backs it —
        # never filename="".
        assert latin1_safe_filename("", fallback='"\x00') == "download"
