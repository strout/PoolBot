{
  description = "PoolBot uv2nix flake";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";
  };

  outputs = { self, nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lib = pkgs.lib;

      # Loading a uv workspace
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      # Picking a Python interpreter
      python = lib.head (pyproject-nix.lib.util.filterPythonInterpreters {
        inherit (workspace) requires-python;
        inherit (pkgs) pythonInterpreters;
      });

      # Constructing a base Python set
      pythonBase = pkgs.callPackage pyproject-nix.build.packages {
        inherit python;
      };

      # Creating a uv2nix generated overlay
      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      # Gluing everything together into a package set
      pythonSet = pythonBase.overrideScope (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          overlay
        ]
      );

      # Virtual environment for production/standard use
      virtualenv = pythonSet.mkVirtualEnv "poolbot-env" workspace.deps.default;

      # Editable overlay for development
      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      editablePythonSet = pythonSet.overrideScope editableOverlay;

      # Virtual environment for development
      devVirtualenv = editablePythonSet.mkVirtualEnv "poolbot-dev-env" workspace.deps.all;

    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          devVirtualenv
          pkgs.uv
        ];

        env = {
          UV_NO_SYNC = "1";
          UV_PYTHON = editablePythonSet.python.interpreter;
          UV_PYTHON_DOWNLOADS = "never";
        };

        shellHook = ''
          unset PYTHONPATH
          export REPO_ROOT=$(git rev-parse --show-toplevel)
        '';
      };

      packages.${system}.default = virtualenv;
    };
}
