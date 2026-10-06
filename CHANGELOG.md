# Changelog

## [0.0.8](https://github.com/GlueOps/toolbox/compare/v0.0.7...v0.0.8) (2026-10-06)


### Features

* mark the toolbox beta and switch off the observability CLIs ([#35](https://github.com/GlueOps/toolbox/issues/35)) ([2905b02](https://github.com/GlueOps/toolbox/commit/2905b026edd127187ba8cda9734f5d39bb4103af))
* one cluster at a time - wipe the login on a cluster change, add reauth (everyone signs in once more after upgrading) ([9faa7e6](https://github.com/GlueOps/toolbox/commit/9faa7e6293a9c0f58a568ee22c164bafa597e8d4))
* platform-aware messages for agents on Linux, Windows (WSL2) and macOS ([#39](https://github.com/GlueOps/toolbox/issues/39)) ([496d5ce](https://github.com/GlueOps/toolbox/commit/496d5ce2d528713d220a15c83530ab5ab1c00612))
* read-only argocd, helm and dyff, and a workdir mount for GitOps deploys ([#31](https://github.com/GlueOps/toolbox/issues/31)) ([1dacdc1](https://github.com/GlueOps/toolbox/commit/1dacdc123a5a26f56ec2c3c7d2fe5fbb1c55f0d8))
* toolbox-app, toolbox-preflight, toolbox-watch and ./toolbox propose ([#33](https://github.com/GlueOps/toolbox/issues/33)) ([a356180](https://github.com/GlueOps/toolbox/commit/a35618058d4c8cf84a618d3f92a91d2ed9d33bdd))


### Bug Fixes

* PR [#37](https://github.com/GlueOps/toolbox/issues/37) review follow-ups - a smoke test that can't pass by accident ([#38](https://github.com/GlueOps/toolbox/issues/38)) ([9da537a](https://github.com/GlueOps/toolbox/commit/9da537a7ea2d02cf74f9108a32926f724e029ad8))


### Documentation

* split README into an AGENTS.md / HUMANS.md router ([#34](https://github.com/GlueOps/toolbox/issues/34)) ([6e51121](https://github.com/GlueOps/toolbox/commit/6e51121679fe8a9d5b0157fd08ca84981dc91893))


### Tests

* host smoke test, LF line endings, supported platforms ([#37](https://github.com/GlueOps/toolbox/issues/37)) ([9a89886](https://github.com/GlueOps/toolbox/commit/9a898866655c77e8ae33ea64d924ec3d29e7011d))

## [0.0.7](https://github.com/GlueOps/toolbox/compare/v0.0.6...v0.0.7) (2026-09-05)


### Bug Fixes

* fill in promtool's server argument, refuse the paths that cannot authenticate ([#29](https://github.com/GlueOps/toolbox/issues/29)) ([d22018d](https://github.com/GlueOps/toolbox/commit/d22018d61460a06c47445d6ce181629d7eb0c0ae))

## [0.0.6](https://github.com/GlueOps/toolbox/compare/v0.0.5...v0.0.6) (2026-09-05)


### Features

* add promtool, logcli and tempo-cli for querying the observability stack ([#27](https://github.com/GlueOps/toolbox/issues/27)) ([48dddb7](https://github.com/GlueOps/toolbox/commit/48dddb78ab2f985bea4eb560e6734149c015c8cf))

## [0.0.5](https://github.com/GlueOps/toolbox/compare/v0.0.4...v0.0.5) (2026-08-31)


### Documentation

* record two known issues operators will hit ([#25](https://github.com/GlueOps/toolbox/issues/25)) ([3e4b39d](https://github.com/GlueOps/toolbox/commit/3e4b39dae8aa30e0cf6ab8002caa713d50d6094c))

## [0.0.4](https://github.com/GlueOps/toolbox/compare/v0.0.3...v0.0.4) (2026-08-30)


### Features

* host-side `toolbox` driver — detect the environment instead of documenting it ([#22](https://github.com/GlueOps/toolbox/issues/22)) ([37f1e4e](https://github.com/GlueOps/toolbox/commit/37f1e4ecb1b24e9c4ced6171ce15764f66f6a891))

## [0.0.3](https://github.com/GlueOps/toolbox/compare/v0.0.2...v0.0.3) (2026-08-30)


### Features

* split login into --begin/--wait so agents get the URL in one command ([#19](https://github.com/GlueOps/toolbox/issues/19)) ([dee8416](https://github.com/GlueOps/toolbox/commit/dee84160a5770696ecda8bdd60b9f6ef35968f2a))

## [0.0.2](https://github.com/GlueOps/toolbox/compare/v0.0.1...v0.0.2) (2026-08-30)


### Features

* trust an extra CA, for networks that intercept TLS ([#18](https://github.com/GlueOps/toolbox/issues/18)) ([5c9aa31](https://github.com/GlueOps/toolbox/commit/5c9aa31a026b0b4d42703d7fa994a2e44483949f))


### Documentation

* lead with a runbook so agents stop exploring before acting ([#17](https://github.com/GlueOps/toolbox/issues/17)) ([8c5eac4](https://github.com/GlueOps/toolbox/commit/8c5eac41afb5103f7bc335a8c9064fb0d4235342))
* make the agent section self-sufficient ([#4](https://github.com/GlueOps/toolbox/issues/4)) ([b8cfb07](https://github.com/GlueOps/toolbox/commit/b8cfb07f6e4706a385ae7a33cd5c71546b64390e))
* move agent instructions to AGENTS.md ([#16](https://github.com/GlueOps/toolbox/issues/16)) ([e16cd3d](https://github.com/GlueOps/toolbox/commit/e16cd3d5f81e1d42ff0d058c2f9239eb72af9995))

## 0.0.1 (2026-08-30)


### Features

* dockerised toolbox with the platform CLIs preauthenticated ([#1](https://github.com/GlueOps/toolbox/issues/1)) ([8c414e4](https://github.com/GlueOps/toolbox/commit/8c414e4152682c3a1d376e7d0abce8fdf04cea46))


### Bug Fixes

* set initial-version so the first release is v0.0.1, not v1.0.0 ([#3](https://github.com/GlueOps/toolbox/issues/3)) ([75d250b](https://github.com/GlueOps/toolbox/commit/75d250b22be210b9c066579f12341058b2f06522))
