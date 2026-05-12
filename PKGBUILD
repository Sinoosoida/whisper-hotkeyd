# Maintainer: Sinoosoida
pkgname=whisper-hotkeyd
pkgver=5.1.0
pkgrel=1
pkgdesc="Push-to-talk voice transcription to clipboard, with system tray"
arch=('any')
url="https://github.com/sinoosoida/whisper-hotkeyd"
license=('MIT')
depends=(
    'python'
    'pyside6'
    'python-evdev'
    'python-pydub'
    'python-numpy'
    'python-requests'
    'python-tomli-w'
    'ffmpeg'
    'alsa-utils'
    'xclip'
    'wl-clipboard'
    'libnotify'
    'xdg-utils'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-setuptools'
    'python-wheel'
)
source=()
sha256sums=()

build() {
    cd "$startdir"
    python -m build --wheel --no-isolation
}

package() {
    cd "$startdir"
    python -m installer --destdir="$pkgdir" dist/whisper_hotkeyd-${pkgver}-py3-none-any.whl

    # Provide a desktop entry for app launchers (not autostart by default;
    # users opt in via `whisper-hotkeyd --setup`).
    install -Dm644 "$startdir/data/whisper-hotkeyd.desktop" \
        "$pkgdir/usr/share/applications/whisper-hotkeyd.desktop"

    # Icon for the desktop entry.
    install -Dm644 "$startdir/whisper_hotkeyd/resources/icon.svg" \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/whisper-hotkeyd.svg"

    # Optional systemd user unit (enable with `systemctl --user enable whisper-hotkeyd`).
    install -Dm644 "$startdir/data/whisper-hotkeyd.service" \
        "$pkgdir/usr/lib/systemd/user/whisper-hotkeyd.service"
}
