# 第 09 章: 任务系统

Agent 循环 (第 01 章) 顺序处理 tool call。子 agent (第 08 章) 在 agent 级别引入并发。但许多实际工作流需要更细粒度的并行 -- 同时运行测试, lint 和构建, 全部完成后再审查结果。任务系统提供这一能力: 一个类型化的后台工作项注册表, 由依赖图连接, 配备 append-only 磁盘输出和五状态生命周期状态机。

## 问题

顺序执行在独立工作上浪费时间。用户请求 "并行运行单元测试, 集成测试和 lint, 然后汇总结果" 时, 期望并发执行与有序后处理。没有任务抽象, agent 只能串行执行, 或生成子 agent 却缺乏结构化方式追踪完成状态, 表达排序约束, 或增量读取输出。

并发问题不止于执行顺序。后台任务持续产生输出。30 秒的测试运行不应阻止 agent 读取部分结果。任务挂起时必须可终止且不丢失已捕获内容。Swarm 场景中多个 agent 同时创建任务时, 注册表必须处理并发写入而不损坏数据。

依赖图增加进一步复杂性。任务 A 依赖任务 B 和 C, B 完成时 A 不应解锁 -- 必须等 C 也完成。图必须双向 (任务知道自己阻塞了谁, 也知道被谁阻塞), 且自维护 (已完成的 blocker 自动消失)。

## Claude Code 的解法

### 任务类型与 ID 生成

七种任务类型覆盖全范围后台工作。每种类型有单字母前缀, 每个任务 ID 由前缀加 8 个随机字母数字字符 (36 字符表) 组成, 产生 36^8 (约 2.8 万亿) 种可能, 碰撞可忽略:

```typescript
// src/tools/task/Task.ts
type TaskType =
  | "local_bash"          // 前缀: b
  | "local_agent"         // 前缀: a
  | "remote_agent"        // 前缀: r
  | "in_process_teammate" // 前缀: t
  | "local_workflow"      // 前缀: w
  | "monitor_mcp"         // 前缀: m
  | "dream";              // 前缀: d

function generateTaskId(type: TaskType): string {
  const prefix = TYPE_PREFIX_MAP[type]; // 如 "b"
  const suffix = randomAlphanumeric(8); // 36^8 种组合
  return prefix + suffix;
}
```

前缀有实际用途: 扫描任务列表时 `b` 开头立即可识别为 bash 任务, `a` 开头为 agent 任务, 无需查表。

### 生命周期状态机

每个任务遵循五状态生命周期。转换严格: `pending` 开始, 执行时进入 `running`, 终止于三个终态之一:

```typescript
// src/tools/task/Task.ts
type TaskStatus = "pending" | "running" | "completed" | "failed" | "killed";

interface TaskStateBase {
  id: string;
  type: TaskType;
  status: TaskStatus;
  description: string;
  outputFile: string;
  outputOffset: number;
  notified: boolean;
  blocks: string[];
  blockedBy: string[];
  // ... 共 12 个字段
}
```

`notified` 字段追踪 agent 是否已被通知完成, 防止重复通知。`outputOffset` 支持增量读取。12 个字段共同捕获工作项的完整生命周期: 身份, 阶段, 输出, 以及与其他任务的关系。

### 磁盘输出 streaming

每个任务写入专用的 append-only 文件。DiskTaskOutput 系统强制 5 GB 上限, 提供基于 offset 的增量读取:

```typescript
// src/tools/task/diskOutput.ts
class DiskTaskOutput {
  append(content: string): void {
    // O_NOFOLLOW 防止符号链接攻击
    // Append-only: 不回退, 不截断
    fs.appendFileSync(this.path, content, { flag: "a" });
  }

  readDelta(offset: number): { content: string; newOffset: number } {
    const fd = fs.openSync(this.path, "r");
    const buf = Buffer.alloc(CHUNK_SIZE);
    const bytesRead = fs.readSync(fd, buf, 0, CHUNK_SIZE, offset);
    fs.closeSync(fd);
    return {
      content: buf.toString("utf-8", 0, bytesRead),
      newOffset: offset + bytesRead,
    };
  }
}
```

基于 offset 的设计意味着读取者不会重新处理旧输出。Agent 检查长时间运行的测试套件时, 只收到上次读取以来的新内容, 上下文消耗与增量而非总量成正比。

### 依赖 DAG

依赖关系形成带双向边的有向无环图。声明任务 A 阻塞任务 B 时, 双方记录同时更新:

```typescript
// src/utils/tasks.ts
function addDependency(tasks: Map<string, TaskState>, from: string, to: string) {
  const blocker = tasks.get(from);
  const blocked = tasks.get(to);
  blocker.blocks.push(to);
  blocked.blockedBy.push(from);
}

function listTasks(tasks: Map<string, TaskState>): TaskView[] {
  const completed = new Set(
    [...tasks.values()]
      .filter(t => t.status === "completed")
      .map(t => t.id)
  );
  return [...tasks.values()].map(t => ({
    ...t,
    blockedBy: t.blockedBy.filter(id => !completed.has(id)),
  }));
}
```

关键细节在 `listTasks`: 已完成的 blocker 在读取时过滤, 而非写入时移除。这就是读取时自动解锁。Agent 列出任务时, 对已完成任务的依赖自动从视图中消失, 被阻塞的任务显示为就绪。

### 文件锁与任务创建

`TaskCreateTool` 生成 ID, 写入初始状态, 在 swarm 场景中先获取文件锁:

```typescript
// src/tools/task/TaskCreateTool.ts
async function createTask(
  type: TaskType, description: string, blockedBy?: string[]
): Promise<TaskState> {
  const id = generateTaskId(type);
  const task: TaskState = {
    id, type, status: "pending", description,
    outputFile: path.join(outputDir, `${id}.out`),
    outputOffset: 0, notified: false,
    blocks: [], blockedBy: blockedBy ?? [],
  };
  await withFileLock(registryPath, async () => {
    const registry = await readRegistry();
    registry.set(id, task);
    await writeRegistry(registry);
  });
  return task;
}
```

### 类型特定终止

`TaskStopTool` 按任务类型派发终止信号。不同类型运行在不同执行上下文: bash 任务是 OS 进程, agent 任务是协作式异步循环, teammate 任务可能在独立 pane:

```typescript
// src/tools/task/TaskStopTool.ts
function stopTask(task: TaskState): void {
  switch (task.type) {
    case "local_bash":
      process.kill(task.pid, "SIGTERM");
      break;
    case "local_agent":
    case "in_process_teammate":
      task.abortController.abort();
      break;
    // ... 七种类型各自的 dispatch
  }
  task.status = "killed";
}
```

### 后台执行

任务在独立线程中运行, 接收 `AbortSignal` 用于协作式取消。线程将输出写入磁盘文件, 即使在终止序列中输出仍继续 streaming, 保证部分结果不丢失。线程执行加磁盘输出使 agent 主循环保持空闲, 可处理其他 tool call。

## 关键设计决策

**单字母前缀而非命名空间字符串。** `b3kx9m2p`, `a7tn4q1w` 一目了然。`local_bash_3kx9m2p` 增加视觉噪音而无额外信息, 七种类型在系统内是公知的。

**读取时自动解锁而非写入时移除。** 写入时移除需要在完成时刻遍历更新所有引用。读取时过滤推迟到消费点, 更简单, 多任务同时完成时无竞态。

**Append-only 磁盘输出而非内存缓冲。** 内存缓冲将任务时长限制在进程 RAM 范围内。5 GB 上限的磁盘输出支持任意长任务。基于 offset 的协议天然幂等。

**Swarm 模式下文件锁。** 多 agent 并发创建任务时, 注册表文件是共享资源。文件锁在 OS 级别序列化写入, 避免数据库或协调服务的复杂度。

## 实际体验

用户请求并行运行测试和 lint 时, agent 创建两个 `local_bash` 任务 (ID 如 `bA3x9m2p` 和 `bK7tn4q1`), 再创建 `local_agent` 任务 (`aR5wz8v3`) 声明两个 bash 任务为 blocker。Bash 任务在后台线程立即执行, 输出 streaming 到磁盘。Agent 周期性读取增量监控进度。两个 bash 任务都 `completed` 后, agent 任务的 `blockedBy` 列表在读取时变空, 审查开始。

任务挂起时可调用 `TaskStopTool` 终止。已捕获的输出保留在磁盘。状态转为 `killed`, 依赖任务可被重新规划。

`dream` 类型用于用户空闲时的推测性后台处理, 优先级低, 新输入到达时可被抢占。`monitor_mcp` 监控 MCP server 连接, 断开时自动重启。`local_workflow` 将多步骤链成一个追踪单元。各类型共享生命周期状态机和依赖图, 但执行语义因性质而异。

## 总结

- 七种任务类型通过单字母前缀 (`b`, `a`, `r`, `t`, `w`, `m`, `d`) 加 8 字符随机后缀生成抗碰撞, 可扫描的 ID。
- 五状态生命周期 (`pending`, `running`, `completed`, `failed`, `killed`) 管理每个任务从创建到终结。
- Append-only 磁盘输出加 offset 增量读取支持长时间运行的任务, 无内存压力, 每任务上限 5 GB。
- 双向依赖 DAG 配合读取时自动解锁, blocker 完成时约束自动解除。
- 文件锁, 类型特定终止 dispatch 和级联删除处理并发创建, 优雅终止和干净移除的运维需求。
