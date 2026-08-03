# 分支合并计划

## 现状
4 个特性分支，3 个基于 main（不含 config-refactor），1 个基于 config-refactor：
- `config-refactor`（基于 main）- Config 收拢 + 死代码删除
- `priority-item-dataclass`（基于 main）- PriorityItem + latent bug 测试
- `cloud-provider`（基于 main）- CloudProvider + handler 合并
- `merge-handlers`（基于 config-refactor）- **跳过**，被 cloud-provider 取代

## 冲突预判
三个分支都改了 `monitor.py`，冲突点：
| 位置 | config-refactor | priority-item-dataclass | cloud-provider |
|------|----------------|------------------------|----------------|
| `SimpleFileMonitor.__init__` | +config/sync_trigger/dispatched_lock | 不改 | clients->providers |
| `handle_file_creation` | os.getenv->monitor.config | tuple->PriorityItem | 合并 115/123 |
| `priority_queue_worker` | 不改 | len(item)->item属性 | if-elif->providers dict |
| `_enqueue_priority` | 不改 | tuple->PriorityItem | 不改 |
| `handle_dir_creation` | os.getenv->monitor.config | 不改 | 两段->循环 |

## 合并顺序（已验证 config-refactor 干净）

### 步骤 1：合并 config-refactor -> main
- `git checkout main && git merge config-refactor`
- 已干跑验证：自动合并无冲突
- 推送 main

### 步骤 2：rebase priority-item-dataclass 到新 main
- `git checkout priority-item-dataclass && git rebase main`
- 预期冲突：`handle_file_creation_115/123` 里 os.getenv vs PriorityItem（不同行，可能自动合并）
- 解决原则：保留 config 注入 + PriorityItem 两套改动
- 跑 `test_priority_item.py`（14 项）+ `test_config_injection.py`（50 项）验证
- 推送（force-with-lease）

### 步骤 3：合并 priority-item-dataclass -> main
- `git checkout main && git merge priority-item-dataclass`
- rebase 后应无冲突

### 步骤 4：rebase cloud-provider 到新 main（最复杂）
- `git checkout cloud-provider && git rebase main`
- 预期冲突及解决：
  1. **`SimpleFileMonitor.__init__`**：合并成 `(self, path, providers, config, sync_trigger=None)`，保留 dispatched_lock + mark_upload_failed
  2. **`handle_file_creation`**：cloud-provider 的合并版要用 `monitor.config.local_root`（不用 os.getenv），retry 用 PriorityItem
  3. **`priority_queue_worker`**：providers dict + PriorityItem 属性访问（cloud-provider 还是 len(item)==4，要改成 item.task is None）
  4. **`handle_dir_creation`**：providers 循环 + `monitor.config.local_root`
  5. **`debounce_worker_thread`**：cloud-provider 清理了死参数，config-refactor 加了 sync_trigger，两者都要保留
  6. **`__main__`**：构造 Config + providers + sync_trigger
- 跑全部测试：`test_provider.py`（16）+ `test_priority_item.py`（14）+ `test_config_injection.py`（50）+ `test_latent_bugs.py`（9）
- 推送（force-with-lease）

### 步骤 5：合并 cloud-provider -> main
- `git checkout main && git merge cloud-provider`
- rebase 后应无冲突
- 推送 main

### 步骤 6：清理
- 删除 `merge-handlers` 分支（本地 + 远程，已被取代）
- 可选：删除已合并的特性分支

## 回退方案
- 任何步骤出错：`git rebase --abort` 或 `git merge --abort`
- 已推送到 main 的合并：`git revert -m 1 <merge_commit>` 反向撤销
- 每步合并前确认测试通过再推

## 验证标准
每步完成后必须通过：
- `python tests/test_config_injection.py`（50 项）
- `python tests/test_priority_item.py`（14 项）
- `python tests/test_provider.py`（16 项）
- `python tests/test_latent_bugs.py`（9 项）
- 不跑 test_client.py（不碰真实 API）
