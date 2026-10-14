#!/usr/bin/env bash
# shellcheck disable=SC2034

iso_name="runneos"
iso_label="RUNNEOS_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"
iso_publisher="Runne OS"
iso_application="Runne OS Live"
iso_version="$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y.%m.%d)"
install_dir="arch"
bootmodes=('bios.syslinux'
           'uefi.systemd-boot')
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86,arm64' '-b' '1M' '-Xdict-size' '1M')
bootstrap_tarball_compression=('zstd' '-c' '-T0' '--auto-threads=logical' '--long' '-19')
kernel_params_x86_64="console=ttyS0,115200"
kernel_params_aarch64="clk_ignore_unused pd_ignore_unused arm64.nopauth"
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/root/.automated_script.sh"]="0:0:755"
  ["/root/.gnupg"]="0:0:700"
  ["/usr/local/bin/choose-mirror"]="0:0:755"
  ["/usr/local/bin/Installation_guide"]="0:0:755"
  ["/usr/local/bin/livecd-sound"]="0:0:755"
  ["/usr/local/bin/runneos-live-user"]="0:0:755"
  # mkarchiso copies the profile's airootfs with --no-preserve=ownership,mode,
  # so every mode-sensitive file has to be listed here or it lands with the
  # default 0644. Without this line the installer wrapper staged by build.sh is
  # not executable on the medium and ./runneos-install fails with EACCES.
  ["/usr/share/runneos/installer/runneos-install"]="0:0:755"
)
