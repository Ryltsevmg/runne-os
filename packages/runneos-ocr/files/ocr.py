#!/usr/bin/python3
"""RunneOS OCR: распознать текст с выбранной области экрана.

Область выбирает slurp, снимок делает grim, текст распознаёт tesseract
(по умолчанию rus+eng). Результат копируется в буфер обмена (wl-copy) и
появляется в коротком уведомлении. Отмена выбора области (Esc в slurp)
завершает работу тихо с кодом 0.
"""
import argparse
import re
import subprocess
import sys

DEFAULT_LANG = "rus+eng"
REGION = re.compile(r"-?\d+,-?\d+ \d+x\d+")


def select_region():
    """Строка области slurp «x,y WxH» или None при отмене."""
    # Без таймаута: темп выделения задаёт пользователь, Esc отменяет.
    result = subprocess.run(["slurp"], capture_output=True, text=True)
    if result.returncode:
        return None
    region = result.stdout.strip()
    return region if REGION.fullmatch(region) else None


def grab(region):
    """PNG выбранной области через grim, снимок читается из stdout."""
    result = subprocess.run(["grim", "-g", region, "-"], capture_output=True, timeout=15)
    if result.returncode or not result.stdout:
        raise RuntimeError("grim не смог снять выбранную область")
    return result.stdout


def recognize(image, lang):
    """Текст изображения через tesseract: изображение на stdin, текст из stdout."""
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", lang],
        input=image, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError("tesseract не смог распознать изображение")
    return result.stdout.decode("utf-8", "replace").strip()


def copy_text(text):
    """Текст в буфер обмена Wayland. wl-copy уходит в фон и держит каналы,
    поэтому вывод собирается в DEVNULL, а не в пайпы — иначе ожидание EOF виснет."""
    result = subprocess.run(
        ["wl-copy"], input=text.encode(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        raise RuntimeError("wl-copy не скопировал текст в буфер обмена")


def notify(message):
    """Короткое уведомление best-effort: недоступность демона не ошибка OCR."""
    try:
        subprocess.run(
            ["notify-send", "-a", "RunneOS", "OCR", message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", default=DEFAULT_LANG,
                        help="языки tesseract (по умолчанию %(default)s)")
    args = parser.parse_args()
    try:
        region = select_region()
    except OSError as error:
        print(f"RunneOS: slurp недоступен: {error}", file=sys.stderr)
        return 1
    if region is None:
        return 0  # Esc в slurp — тихий выход без уведомлений
    try:
        text = recognize(grab(region), args.lang)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"RunneOS: {error}", file=sys.stderr)
        return 1
    if not text.strip():
        notify("Текст не распознан")
        return 0
    try:
        copy_text(text.strip())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"RunneOS: {error}", file=sys.stderr)
        return 1
    notify(f"Распознано {len(text)} символов")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130  # Ctrl+C прерывает ожидание выбора, как и Esc
    raise SystemExit(code)
