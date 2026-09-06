{ pkgs }: {
  # Keep this file compatible with Replit's legacy Nix environment used by Publishing.
  # Do not use pkgs.python311 here; that attribute is unavailable in the legacy nixpkgs set.
  deps = [
    pkgs.bash
    pkgs.python3
    pkgs.python3Packages.pip
    pkgs.aria2
    pkgs.gitMinimal
    pkgs.cmake
    pkgs.gperf
    pkgs.zlib
    pkgs.openssl
    pkgs.gcc
    pkgs.gnumake
  ];
}
