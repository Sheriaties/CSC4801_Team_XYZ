---
tags:
  - CSC4810
  - Final_project
  - 中文版本
---

# 期末项目中使用Docker

每个团队存储库**必须**包含一个`Dockerfile`，以便任何人都可以从`main`上的任何提交构建和运行完整环境。图像从源重建：**没有图像tar要上传**。分级员和审计员直接从存储库构建，包括每次错误修复后。

## 您的存储库必须提供的内容

- 一个`Dockerfile`在`INSTALL.md`中记录的位置。
- 入口点脚本、迁移和播种脚本提交到存储库。
- 在`INSTALL.md`中精确的构建、启动、迁移、播种和单元测试命令。
- 记录的图像、配置和任何依赖项的启动命令。

构建**必须**从干净检出成功，不需要无记录的命令或机密。运行环境**必须**复现确定性播种数据。

## 构建和运行

示例命令（用团队记录的图像名称、端口和应用程序命令替换）：

```bash
docker build -t team-example .
docker run -d --name team-example -p 127.0.0.1:8080:8080 team-example
docker exec team-example <migration-command>
docker exec team-example <seed-command>
docker exec team-example <unit-test-command>
```

`INSTALL.md`还必须记录如何启动任何必需的依赖项以及如何持久化或重置本地数据。分级员必须能够遵循该文档而不猜测命令、图像或机密。

## 审计另一个团队

审计员从目标团队的**`main`分支的最新提交**构建。目标团队的所有开发者将其更改和修复发布到`main`。审计员仅发现和报告错误；目标团队负责修复它们。在审计前始终更新到最新提交以避免报告过时的错误：

```bash
git clone <target-repo>
cd <target-repo>
git checkout main
git pull
docker build -t audit-target .
```

然后按照团队的`INSTALL.md`启动、迁移、播种和运行单元测试。在每份报告中记录确切的提交SHA。不要修改目标团队的源或存储库；详见同行评审指南中的安全边界。
