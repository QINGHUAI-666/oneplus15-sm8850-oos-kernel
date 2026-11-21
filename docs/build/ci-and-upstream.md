# 项目分析与 CI / 上游更新设计

> 面向 `QINGHUAI-666/oneplus15-sm8850-oos-kernel`（fork of `travismills82/oneplus15-sm8850-oos-kernel`）
> 日期 2026-11-21 · 基线上游 `f8e4923a0bc5` · 适配提交 `plk110: adapt ColorOS 17.0.1.201 firmware identity`

---

## 1. 项目到底是什么

### 1.1 形态

不是一个普通内核仓库，而是 **repo 清单仓库 + 完整源码合体**：

```
oneplus_15.xml                 # 33 个 repo 项目的清单（AOSP GKI / CodeLinaro / OnePlusOSS）
kernel_platform/
├── common/      87,418 文件 1.7 GB   ACK 6.12.117（含 drivers/kernelsu 与 SUSFS 钩子）
├── soc-repo/     2,042 文件  31 MB   高通构建配置、modules.list、kleaf 脚本
├── qcom/         5,816 文件  32 MB   devicetree bindings
├── oplus/      72+ 文件     119 MB   OnePlus 构建集成
├── build/        639 文件   4.9 MB   kleaf（大量符号链接）
├── external/  19,978 文件   136 MB   bazel 规则、lz4/zlib/toybox…
├── common-modules/trusty
└── prebuilts/*                       ❌ 10 个项目不在仓库里
vendor/        14,727 文件   441 MB   qcom/oplus/nxp/st 驱动与设备树
tools/ docs/                        校验脚本与验证记录
```

- 检出后 **4.4 GB**，`.git` 1.5 GB，149,318 个 tree 条目，**1,015 个符号链接**（893 可用 / 122 悬空，其中 118 个指向缺失的 `prebuilts/*`）。
- 只有 23/33 个 manifest 项目在仓库内；**10 个 `prebuilts/*` 必须 `repo sync` 才有**，缺了直接编不了。

### 1.2 构建图（真正决定 CI 形态的部分）

```
repo sync ──► prebuilts/clang 19.0.1 (14043575) + build-tools + rust + bindgen
                     │
        tools/bazel build //common:kernel_aarch64
                     │        ├─ gki_defconfig（本地改动：ATV、KSU/SUSFS、Droidspaces 前置）
                     │        ├─ gki/aarch64/abi.stg + 40 个 KMI 片段（含 qcom/oplus）
                     │        └─ certs/*.pem（模块签名信任锚）
                     ▼
              Image + Module.symvers
                     │
     //soc-repo:canoe_perf_avb_sign_boot_image  (android_build.bzl)
                     ▼
        boot.img（100,663,296 B，header v4，avbtool add_hash_footer）
```

关键约束：

| 约束 | 值 | 影响 |
|---|---|---|
| 页大小 | 4K（`CONFIG_LOCALVERSION="-4k"`） | 不能用 `kernel_aarch64_16k` |
| KMI 裁剪 | `trim_nonlisted_kmi=True` + 40 片段并集 | 裁掉表外符号即模块拒载 |
| CRC 来源 | `GENDWARFKSYMS` + clang 生成的类型串 | 换编译器 → CRC 变 → 模块拒载 |
| boot 容器 | 0x6000000、header v4、cmdline 空 | 否则分区写不下/引导失败 |
| 产物范围 | 一次构建会产出 boot/vendor_boot/init_boot/vendor_dlkm/system_dlkm/dtbo | 只允许刷 boot |

### 1.3 资源画像（决定能不能用 GitHub 托管 runner）

| 阶段 | 磁盘 | 说明 |
|---|---|---|
| repo sync（含 prebuilts） | 10–14 GB | clang 工具链 2–3 GB、rust ~1 GB、ndk ~1 GB、源码 ~6 GB |
| bazel 输出 | 15–25 GB | `bazel-bin` + `bazel-out` |
| kbuild/ccache 缓存 | 10–20 GB | `--cache_dir`（作者增量构建 204 s 就靠它） |
| **合计** | **≈ 40–60 GB**，建议 ≥ 200 GB 留余量 | |

GitHub 托管 `ubuntu-latest`：4 vCPU / 16 GB RAM，官方规格写的是 **14 GB SSD**——但那是**保证配额**，不是实际容量：
runner 的根卷约 84 GB，初始空闲 29–30 GB，用 free-disk-space 类清理（Android SDK、.NET、GHC、CodeQL、Boost、docker 镜像）
可再释放 **30–36 GB（激进 ~37–44 GB）**（[规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[清理工具说明](https://github.com/ggml-org/free-disk-space)）。

→ 实际可用约 **55–70 GB**，**可以**跑裁剪后的构建（源码 ~14 GB + bazel out ~20 GB），代价是 4 核下 40–70 分钟。
所以 `build-kernel.yml` 提供两个 runner 选项：

| 选项 | 适用 | 特点 |
|---|---|---|
| `self-hosted`（默认，标签 `op15`） | 反复构建 | 保留 prebuilts 与 bazel 缓存，增量 ~3.5 分钟 |
| `ubuntu-latest` | 临时/无自建机 | 需先清理磁盘（workflow 已内置），首建 40–70 分钟 |

作者实测：首次全量 ~1,436 s（4 线程冷缓存），增量 204 s。

### 1.4 现有 CI 与痛点

仓库自带一个 `.github/workflows/controlled-stack-release-validate.yml`（发布物校验）。
缺少的是：

1. 没有“推代码就编译”的流水线（作者本地构建）；
2. 没有 KMI 契约门禁（构建完是否还能喂 stock 模块，靠人肉）；
3. 两个第三方特性（KernelSU、SUSFS）**没有上游追踪**——靠作者某天手动 `ANDROID: update SUSFS to v2.4.0`。

---

## 2. 第三方特性的真实形态（决定自动化能做到什么程度）

### 2.1 KernelSU：整体 vendored

```
kernel_platform/common/drivers/kernelsu/     # 14 个子目录，等于上游 kernel/ 全部内容
  Kbuild:  "# Vendored upstream 28db74f6... has 2716 ancestors"
           "KSU_VERSION := 32716"            # 上游公式 30000 + git rev-list --count
```

- 上游：`tiann/KernelSU`（`kernel/` 目录结构逐项对应，含 `preinit/`、`supercall/`、`sulog/`）
- 当前 pin：`28db74f6f4692e97006ce9ebd8b19b9cc614b5b7` → 32716（实测 `rev-list --count` = 2716，与仓库一致 ✅）
- 上游 head：`ff70c90d6de4…`（2026-11-19），最新 tag `v3.3.1` = `0c0fdb96…`
- **可完全自动化**：替换目录 → 重算版本 → 重打 SUSFS 的 KernelSU 补丁。

### 2.2 SUSFS：补丁式，且会改 KernelSU

```
上游 simonpunk/susfs4ksu @ gki-android16-6.12
├── kernel_patches/50_add_susfs_in_gki-android16-6.12.patch   # 27 文件、~109 KB
├── kernel_patches/fs/susfs.c                                 # 整文件
├── kernel_patches/include/linux/susfs{,_def}.h               # 整文件
└── kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch     # ~150 KB，改 KernelSU 源码
```

本树现状：`SUSFS_VERSION "v2.4.0"`，pin 到 `f83ae68a…`（2026-11-08），
实际触及 **39 个文件**（`fs/`、`security/selinux/`、`mm/`、`kernel/`、`drivers/kernelsu/`）。

上游 head `9b0b13f0…`（2026-11-15）比本地多 `sus_path_uid_filter` 等特性 → **确实需要更新**。

**实测**（在 `kernel_platform/common` 的副本上试跑上游 head 补丁）：

| 结果 | 数量 | 处理 |
|---|---|---|
| 已应用（`Reversed/previously applied`） | 22 文件 | 自动忽略并删除 `.rej` |
| 真实冲突 | 5 文件（`mm/memory.c`、`fs/readdir.c`、`fs/namei.c`、`security/selinux/hooks.c`、`security/selinux/ss/services.c`） | 需人工 3-way |

→ 结论：**KernelSU 可以全自动；SUSFS 只能“自动到冲突为止”**，冲突必须人看。这是设计的前提，不是实现缺陷。

---

## 3. CI 设计（三个 workflow）

### 3.1 `static-checks.yml` — 托管 runner，每次 push/PR

| 检查 | 依据 |
|---|---|
| AVB fingerprint / security_patch / os_version | `tools/plk110/stock_identity.json` |
| 模块签名证书 DER 哈希 = stock Image 里那张 | `openssl x509 -outform DER \| sha256` |
| oracle 完整性（1,069 模块 / 4,267 符号） | `stock_module_oracle.json` |
| KernelSU 版本 = `pins.json`，且 Kbuild 记录了 commit | `drivers/kernelsu/Kbuild` |
| SUSFS 版本 = `pins.json` | `include/linux/susfs.h` |
| 所有 Python 工具可编译 | `compileall` |

执行体：`tools/plk110/selfcheck.py`（本地同样可跑，已通过）。

### 3.2 `build-kernel.yml` — self-hosted（`[self-hosted, linux, x64, op15]`）

- 触发：`workflow_dispatch`（选 variant / target / jobs）+ 推 `plk110-*` 分支且改动 `kernel_platform/**`
- 预检：磁盘 ≥ 80 GB、`repo` 存在、Python ≥ 3.10
- `repo init -u <fork> -b <variant> -m oneplus_15.xml --depth=1` → `repo sync -c -j8`
- 校验 prebuilts 三个关键目录存在（防止半同步）
- 构建 `//common:kernel_aarch64` + ABI/KMI 检查；可选 `//soc-repo:canoe_perf_avb_sign_boot_image`
- **门禁**：`boot.img` 必须 100,663,296 B；`verify_image.py` 必须 PASS（4,267/4,267）
- 产物：`Image`、`Module.symvers`、`boot.img`、`SHA256SUMS`、`gate.txt`

### 3.3 `upstream-watch.yml` — 托管 runner，每天 03:17 UTC

```
check ──无漂移──► 结束（写 summary）
  │
  └─有漂移─► sync-kernelsu ─► sync-susfs
                    │              │
                    └──都成功──► 建分支 chore/upstream-sync-YYYYMMDD
                    │             推 git，开 draft PR（附 report.md）
                    │
                    └──任一冲突──► 开 issue（附冲突报告 + 上游 head 表）
```

- **永不直推 main**，全部走 PR；
- `tools/upstream/cache/` 在提交前删除（补丁缓存不进库）；
- 失败时把 `report.md`（逐 hunk 的 `.rej` 片段）放进 issue 正文，并上传 artifact。

---

## 4. 上游更新工具（`tools/upstream/upstream.py`）

```bash
python3 tools/upstream/upstream.py check                     # 漂移报告，exit 1 = 有漂移
python3 tools/upstream/upstream.py sync-kernelsu [--ref R] [--dry-run]
python3 tools/upstream/upstream.py sync-susfs    [--ref R] [--dry-run]
```

`pins.json` 是唯一事实来源：

```json
{
  "kernelsu": {"upstream": "tiann/KernelSU", "ref": "28db74f6…", "version": 32716,
               "source_subdir": "kernel", "target": "kernel_platform/common/drivers/kernelsu"},
  "susfs":    {"upstream": "simonpunk/susfs4ksu", "branch": "gki-android16-6.12",
               "ref": "f83ae68a…", "version": "v2.4.0",
               "kernel_patch": "kernel_patches/50_add_susfs_in_gki-android16-6.12.patch",
               "ksu_patch": "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch",
               "owned_files": ["kernel_patches/fs/susfs.c->fs/susfs.c", …]}
}
```

### sync-kernelsu 做什么（三方合并，不是整目录替换）

整目录替换会丢掉作者本地适配（SUSFS 钩子、KABI 修正、固定版本块），所以实际做法是**文件级三方合并**：

```
base   = 上游 <pinned ref> 的 kernel/
ours   = 树里的 drivers/kernelsu/
theirs = 上游 <new ref> 的 kernel/
```

- `theirs == base` → 上游没动，保留本地；
- `ours == base` → 本地没动，直接取上游；
- 两边都动 → `git merge-file -p ours base theirs`，返回码非 0 即冲突。

随后写入 `KSU_VERSION := 30000 + rev-list --count <ref>`（上游靠 git 推导，vendored 副本没有 .git），
校验 `Kconfig` 仍含 `KSU_SUSFS`，最后更新 `pins.json`。

**实测**（在真实仓库上跑）：

| 场景 | 结果 |
|---|---|
| 同 ref 重跑（幂等性） | 0 文件变更 ✅ |
| 升到上游 head `ff70c90d` | 2734 commits → 版本 32734；**2 处真冲突**：`feature/selinux_hide.c`、`selinux/rules.c`（都是 SUSFS 钩子与上游新代码重叠）→ 退出 2 + 写报告 |

### sync-susfs 做什么

1. 从 GitLab 拉三个“自有文件”（`fs/susfs.c`、`susfs.h`、`susfs_def.h`）整文件覆盖
2. `patch -p1 --forward` 应用内核补丁；**已应用的 hunk 自动识别并删除 `.rej`**
3. 应用 KernelSU 补丁
4. 真实冲突 → 生成 `tools/upstream/report.md`，退出 2（CI 转 issue）

---

## 5. 使用与运维

```bash
# 本地检查漂移
python3 tools/upstream/upstream.py check

# 本地手动同步（不动 CI）
python3 tools/upstream/upstream.py sync-kernelsu
python3 tools/upstream/upstream.py sync-susfs

# 构建（self-hosted runner 上）
gh workflow run build-kernel.yml -f variant=plk110-17.0.1.201-kernelsu-susfs -f target=kernel+boot
```

**self-hosted runner 准备**

```bash
sudo apt-get install -y git python3 curl
curl -sSL https://storage.googleapis.com/git-repo-downloads/repo > ~/bin/repo && chmod +x ~/bin/repo
# 打标签：op15 + self-hosted + linux + x64
./config.sh --url https://github.com/QINGHUAI-666/oneplus15-sm8850-oos-kernel --token <TOKEN> \
            --labels op15 --name op15-builder
```

---

## 6. 风险与边界

| 项 | 说明 |
|---|---|
| 托管 runner 编不了 | 磁盘 14 GB < 需求 40–60 GB；已在 workflow 注释与本文档写明 |
| SUSFS 自动同步常需人工 | 实测 5 个文件真冲突；自动化只保证“不产出半成品” |
| KernelSU 大版本跳跃风险 | 上游可能改 UAPI（32716 → 新版），Manager 必须同 UAPI；同步后要复测 GET_INFO |
| CRC 契约门禁 | `verify_image.py` 只覆盖 MODVERSIONS；struct 布局仍需作者的完整 oracle |
| 定时任务依赖网络 | GitHub/GitLab API 限流时 `check` 会失败并标红，不误报漂移 |
| 补丁缓存 | `tools/upstream/cache/` 已被 workflow 删除，且不应提交 |
