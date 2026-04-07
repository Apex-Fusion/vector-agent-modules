{ pkgs ? import <nixpkgs> {} }:

let
  # Aiken - musl-linked static binary (works on NixOS without patching)
  aiken = pkgs.stdenv.mkDerivation rec {
    pname = "aiken";
    version = "1.1.21";

    src = pkgs.fetchurl {
      url = "https://github.com/aiken-lang/aiken/releases/download/v${version}/aiken-x86_64-unknown-linux-musl.tar.gz";
      hash = "sha256-n+C7lO+7efTTUWZN7z2g1Yx+MHFvy2VZD/m2xrD/txU=";
    };

    sourceRoot = "aiken-x86_64-unknown-linux-musl";

    installPhase = ''
      mkdir -p $out/bin
      cp aiken $out/bin/
      chmod +x $out/bin/aiken
    '';
  };

  pythonEnv = pkgs.python311.withPackages (ps: with ps; [
    httpx
    pydantic
    python-dotenv
    cbor2
    pytest
    pytest-asyncio
    pip
  ]);
in
pkgs.mkShell {
  buildInputs = [
    aiken
    pythonEnv
    pkgs.jq
    pkgs.curl
    pkgs.git
  ];

  shellHook = ''
    echo "=== Vector Module 6: Governance Suggestion Engine ==="
    echo ""
    echo "  Aiken:   $(aiken --version 2>/dev/null || echo 'checking...')"
    echo "  Python:  $(python --version)"

    # Create venv for pip-only packages (pycardano, vector_agent)
    if [ ! -d .venv ]; then
      python -m venv .venv --system-site-packages
      .venv/bin/pip install -q /home/david/code/agent-sdk-py 2>/dev/null
    fi
    export PATH="$PWD/.venv/bin:$PATH"
    export VIRTUAL_ENV="$PWD/.venv"
    export PYTHONPATH="$PWD:$PYTHONPATH"

    echo ""
    echo "Quickstart:"
    echo "  cd contracts/governance-suggestion && aiken build"
    echo "  cd contracts/governance-suggestion && aiken check"
    echo ""
  '';
}
