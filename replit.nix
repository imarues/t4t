{ pkgs }: {
  deps = [
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
