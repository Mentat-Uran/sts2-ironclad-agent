# STS2MCP bridge compatibility

This project uses the game-side C# Mod and REST bridge from [Gennadiyev/STS2MCP](https://github.com/Gennadiyev/STS2MCP). The bridge source is separate from this Ironclad controller. The project patch is based on upstream commit `55e064850a68f3b4cde7e5fd525bf9b2dec4e885` and is stored in `patches/STS2MCP-v0.4.4.patch`; it is not an upstream release.

## Patch scope

The patch keeps passive bridge schema 2 and updates the Mod manifest to 0.4.4. It makes shop, treasure, and card-selection reads/actions passive and verifiable, adds the current `player.deck` data used for strategic card picks, and aligns treasure-room proceed checks with the visible, enabled control. Card selection still requires a fresh state read after every submitted action.

The controller checks the bridge's harmless root response before requesting game state. It rejects bridge versions that do not advertise the required passive schema. This protects against older snapshot builders that could open a shop inventory or chest as a side effect of reading state.

## Recreate the patched upstream checkout

The repository intentionally does not publish third-party source checkouts or compiled game Mod files. From a clean clone, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-sts2mcp.ps1
& .\vendor\STS2MCP\build.ps1 -GameDir $env:STS2_GAME_DIR
```

The bootstrap script pins the upstream commit, verifies the patch applies, and leaves the checkout in `vendor/STS2MCP/`. The upstream MIT license is included in [third-party notices](../THIRD_PARTY_NOTICES.md).

The build requires the game assemblies from a local installation and a compatible .NET SDK. This repository does not include those assemblies or the compiled DLL.

## Version boundary and installer

The patch was prepared against Steam Main `v0.107.1` and bridge schema 2. The controller checks the installed `release_info.json` when `game_dir` is configured and requires the configured version to match. Recheck the game and Mod after any game update; community or Beta data must not be silently treated as Main.

The guarded installer requires an explicit game directory and an existing STS2MCP Mod identity before replacing the Mod DLL/manifest. It refuses to run while the game is open and verifies the target files afterward. It does not install game files, alter saves, or modify unrelated Mods.
