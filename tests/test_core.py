"""End-to-end tests for media-context using synthetic ffmpeg assets."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from media_context.cache import CacheStore
from media_context.hook import evaluate_hook
from media_context.mcp_server import handle_rpc_message
from media_context.media_ops import make_contact_sheet, shrink_image
from media_context.probe import probe_media


def _make_test_assets(tmp: Path) -> tuple[Path, Path, list[Path]]:
    video = tmp / "sample.mp4"
    audio = tmp / "sample.wav"
    imgs: list[Path] = []

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(video),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            str(audio),
        ],
        check=True,
    )
    for i in range(4):
        img = tmp / f"frame_{i}.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=0x{i*40+20:02x}80c0:s=1920x1080",
                "-frames:v", "1", str(img),
            ],
            check=True,
        )
        imgs.append(img)
    return video, audio, imgs


class TestMediaContext(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="mctx_test_")
        self.tmp = Path(self._td.name)
        os.environ["MCTX_HOME"] = str(self.tmp / "cache")
        self.store = CacheStore()
        self.video, self.audio, self.imgs = _make_test_assets(self.tmp)

    def tearDown(self) -> None:
        os.environ.pop("MCTX_BYPASS", None)
        self._td.cleanup()

    def test_probe_video_and_audio(self) -> None:
        vinfo = probe_media(self.video, check_audio=True, check_black=True, store=self.store)
        self.assertEqual(vinfo["kind"], "video")
        self.assertEqual(vinfo["video"]["width"], 640)
        self.assertIn("audio_check", vinfo)
        self.assertIn("black_check", vinfo)

        ainfo = probe_media(self.audio, check_audio=True, store=self.store)
        self.assertEqual(ainfo["kind"], "audio")
        self.assertIn("mean_volume_db", ainfo["audio_check"])

    def test_shrink_and_contact_sheets(self) -> None:
        sh = shrink_image(self.imgs[0], max_side=640, store=self.store)
        self.assertTrue(Path(sh["output"]).exists())

        sv = make_contact_sheet([self.video], grid="2x2", width=640, store=self.store)
        self.assertEqual(sv["mode"], "video")
        self.assertTrue(Path(sv["output"]).exists())

        si = make_contact_sheet(self.imgs, grid="2x2", width=640, store=self.store)
        self.assertEqual(si["mode"], "images")
        self.assertEqual(si["sampled_count"], 4)
        self.assertTrue(Path(si["output"]).exists())

    def test_hook_block_and_bypass(self) -> None:
        code, _, err = evaluate_hook({"tool_input": {"Path": str(self.audio)}}, client="antigravity", mode="block")
        self.assertEqual(code, 2)
        self.assertIn("mctx probe", err)

        code, _, err = evaluate_hook({"tool_input": {"file_path": str(self.imgs[0])}}, client="claude", mode="block")
        self.assertEqual(code, 2)
        self.assertIn("mctx inspect", err)

        # Whitelisted contact sheet should pass
        sv = make_contact_sheet([self.video], grid="2x2", width=640)
        code, _, _ = evaluate_hook({"tool_input": {"Path": sv["output"]}}, client="antigravity", mode="block")
        self.assertEqual(code, 0)

        # MCTX_BYPASS=1 should allow everything
        os.environ["MCTX_BYPASS"] = "1"
        code, _, _ = evaluate_hook({"tool_input": {"Path": str(self.audio)}}, client="antigravity", mode="block")
        self.assertEqual(code, 0)

    def test_mcp_protocol(self) -> None:
        init = handle_rpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "media-context")

        tools = handle_rpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in tools["result"]["tools"]}
        self.assertIn("media_probe", names)
        self.assertIn("media_inspect", names)

        call = handle_rpc_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "media_probe", "arguments": {"path": str(self.video)}},
            }
        )
        self.assertFalse(call["result"]["isError"])
        self.assertIn('"kind": "video"', call["result"]["content"][0]["text"])

    def test_stats(self) -> None:
        probe_media(self.video, store=self.store)
        stats = self.store.get_stats()
        self.assertGreaterEqual(stats["total_events"], 1)
        self.assertGreater(stats["saved_tokens_est"], 0)


if __name__ == "__main__":
    unittest.main()
