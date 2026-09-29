{
  description = "Simple Video Editor - annotate and assemble short videos";

  inputs = {
    # nixpkgs-25.11-darwin as of 2026-08-31. The previous pin
    # (a7fc11be66bd) had no aarch64-darwin binary cache for pyside6 and its
    # qtconnectivity 6.10.0 failed to compile on darwin (pcsclite headers not
    # on the include path), which broke `nix develop`/`nix run` on macOS.
    # This commit ships the same stack (Qt 6.10, PySide6 6.10.0, ffmpeg 7.1,
    # Python 3.12) with darwin binaries cached.
    nixpkgs.url = "github:NixOS/nixpkgs/0921fdb3e13e40fe25fbc52b89661a9d6d32ac68";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;

        # ffmpeg licensing: we bundle the *GPL* build (libx264, libx265).
        # Rationale in README.md#licensing. Short version: the spec's encoder
        # settings (-c:v libx264 -crf -preset) require libx264, which is GPL;
        # this app invokes ffmpeg as a separate process over a command line
        # rather than linking it, so the app's own source is not a derivative
        # work. Redistribution obligation is to ship ffmpeg's corresponding
        # source, which `nix build .#ffmpeg-source` produces.
        # The `headless` variant: same libraries, same filters, same encoders,
        # but no ffplay and so no SDL / GTK / zenity / gst-plugins-bad in the
        # closure. We only ever invoke ffmpeg and ffprobe as subprocesses, so
        # none of that was reachable - it was just several hundred megabytes of
        # an AppImage.
        ffmpeg = pkgs.ffmpeg_7-headless.override {
          withGPL = true;
          withUnfree = false;
          withX264 = true;
          withX265 = true;
        };

        qtDeps = with pkgs.qt6; [ qtbase qtmultimedia qtsvg ]
          ++ pkgs.lib.optional pkgs.stdenv.hostPlatform.isLinux pkgs.qt6.qtwayland;

        pythonEnv = python.withPackages (ps: with ps; [ pyside6 pytest ]);

        fonts = pkgs.dejavu_fonts;

        # Runtime environment shared by the packaged app and `nix run`.
        # Resolved at build time to absolute store paths, so nothing depends on
        # PATH or on the user's fontconfig.
        runtimeEnv = [
          "--set" "SVE_FFMPEG" "${ffmpeg}/bin/ffmpeg"
          "--set" "SVE_FFPROBE" "${ffmpeg}/bin/ffprobe"
          "--set" "SVE_FONT_DIR" "${fonts}/share/fonts/truetype"
        ];

        simple-video-editor = python.pkgs.buildPythonApplication {
          pname = "simple-video-editor";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ python.pkgs.setuptools ];
          dependencies = [ python.pkgs.pyside6 ];

          nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook pkgs.makeWrapper ];
          buildInputs = qtDeps;

          # buildPythonApplication wraps the entry points for Python; the Qt
          # hook then needs to wrap them again for QT_PLUGIN_PATH. dontWrapQtApps
          # plus an explicit wrapProgram keeps both sets of arguments on one
          # wrapper instead of nesting them.
          dontWrapQtApps = true;

          postFixup = ''
            for program in $out/bin/simple-video-editor $out/bin/sve; do
              wrapProgram "$program" \
                ''${qtWrapperArgs[@]} \
                ${pkgs.lib.escapeShellArgs runtimeEnv}
            done
          '';

          # The test suite drives a real Qt application and shells out to
          # ffmpeg; it runs from the devShell (`nix develop -c pytest`), not in
          # the sandbox, which has neither a display nor the generated fixtures.
          doCheck = false;

          meta = with pkgs.lib; {
            description = "Annotate and assemble short videos";
            mainProgram = "simple-video-editor";
            platforms = platforms.unix;
            # The bundled ffmpeg is the GPL build (libx264). See README.
            license = licenses.gpl2Plus;
          };
        };

      in {
        packages = {
          default = simple-video-editor;
          inherit simple-video-editor ffmpeg;
          # `nix build .#ffmpeg-source` produces the corresponding source for
          # the bundled GPL ffmpeg, which redistribution requires.
          ffmpeg-source = ffmpeg.src;
        };

        apps.default = {
          type = "app";
          program = "${simple-video-editor}/bin/simple-video-editor";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            ffmpeg
            pkgs.qt6.qttools
            pkgs.dejavu_fonts
            pkgs.ruff
          ] ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
            # End-to-end GUI tests render into a throwaway X server. A real
            # X server rather than Qt's `offscreen` platform, because
            # offscreen does not exercise QVideoWidget's actual compositing
            # path - which is precisely what those tests check.
            pkgs.xorg.xorgserver
            pkgs.xorg.xdpyinfo
          ];
          nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook pkgs.makeWrapper ];
          buildInputs = qtDeps;

          # wrapQtAppsHook only prepares `qtWrapperArgs`; in a devShell there is
          # no binary to wrap, so materialise those args into the shell env.
          # This is the idiom from the nixpkgs Qt documentation.
          shellHook = ''
            setQtEnvironment=$(mktemp)
            makeShellWrapper "$(type -p sh)" "$setQtEnvironment" "''${qtWrapperArgs[@]}"
            sed "/^exec/d" -i "$setQtEnvironment"
            source "$setQtEnvironment"

            export SVE_FFMPEG=${ffmpeg}/bin/ffmpeg
            export SVE_FFPROBE=${ffmpeg}/bin/ffprobe
            export SVE_FONT_DIR=${pkgs.dejavu_fonts}/share/fonts/truetype
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      });
}
