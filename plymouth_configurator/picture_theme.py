"""Create a self-contained boot theme from an ordinary image."""

from pathlib import Path
import shutil
import tempfile
import uuid
import warnings

from PIL import Image, ImageOps


def set_picture_fit(theme: Path, fill: bool) -> None:
    script = Path(__file__).with_name("picture.script").read_text()
    (theme / "picture.script").write_text(script.replace("@FILL_SCREEN@", "1" if fill else "0"))


def create_picture_theme(image: Path, cache_dir: Path, fill: bool = True) -> Path:
    """Return a unique theme directory; its temporary parent belongs to the caller."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="picture-", dir=cache_dir))
    name = "picture-" + uuid.uuid4().hex[:12]
    theme = work / name
    try:
        theme.mkdir()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image) as original:
                # Apply camera orientation, discard metadata and use the first frame.
                rgba = ImageOps.exif_transpose(original).convert("RGBA")
                rgba.thumbnail((3840, 2160), Image.Resampling.LANCZOS)
                background = Image.new("RGB", rgba.size, "black")
                background.paste(rgba, mask=rgba.getchannel("A"))
                background.save(theme / "background.png", optimize=True)
        Image.new("RGBA", (1, 1), (0, 0, 0, 220)).save(theme / "password-panel.png")
        title = " ".join(image.stem.split()) or "My picture"
        (theme / f"{name}.plymouth").write_text(
            f"[Plymouth Theme]\nName=Picture: {title}\n"
            "Description=A personal picture boot screen\nModuleName=script\n\n"
            "[script]\nImageDir=.\nScriptFile=picture.script\n", encoding="utf-8")
        set_picture_fit(theme, fill)
        return theme
    except BaseException:
        shutil.rmtree(work)
        raise
