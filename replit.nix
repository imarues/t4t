{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.python311Packages.pip
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
