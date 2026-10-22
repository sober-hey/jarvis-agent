# 重复提问修复上线审批（2026-10-20 16:50）

## 版本信息
- 提交：`d6341f62`（本次修改未提交）
- 子代理完成时间：2026-10-20 15:48:19
- 主脑评审完成：2026-10-20 16:50

## 修复内容
4 处缺陷修复，含 4 个新测试。

| 位置 | 修改说明 |
|------|---------|
| inner_runtime.py:1023 | 从 mind.db + sir_profile.db 读全量 self_report，不从空 memory.db 获取 |
| daemon_wiring.py:bounded_context | 按 (同主题, source优先级, importance) 排序裁剪，保护自述和同主题信念 |
| outreach.py:433-445 | 按 (belief_id, gap_subject, gap_attribute) 去重，不按 reason |
| inner_runtime.py:route | 按 belief_id + gap_attribute 去重，允许同信念不同查询方向 |

## 测试验收
- 新测试 4/4 passed
- 必跑 11 套 + 新增：209 passed, 375 subtests passed
- Ruff format/lint：格式通过；lint 同原版 12 条既有、新增 0 条
- Pyright strict：原版 9 errors → 新版 9 errors（未改进，不能声称全绿）

## 负向对照验证
逐个拆回原函数：4 个新测试各失败 1 项（合共 4 failed），证明每个测试独立捕获对应缺陷。

## 线上影响
- **已知修复**：职业重复提问（邮件草稿、网页、awaiting_sir）
- **遗留事项**：历史 awaiting_sir 状态不自动恢复；需到常驻服务启动后重新测试全流程

## 上线许可

✓ **代码评审通过**
✓ **功能测试通过**
✓ **负向对照有效**
⚠ **Pyright strict 未全绿**（不是本次引入，不作阻塞条件）

**上线决议**：允许应用到产品库。不合并到主分支（Pyright 未全绿），改为标记待验收，常驻服务启动后需人工确认线上行为无回归。

---

**关键检查清单（启动常驻服务前）**：
- [ ] 职业重复提问不再出现（邮件、网页、iMessage）
- [ ] 其他问题独立投递无压制（至少 3 种不同的空白各投一次）
- [ ] 同一问题 6 小时内去重有效（手动触发重复）
- [ ] 无新的错误日志或 awaiting_sir 残留

---

主脑：Hermes  
日期：2026-10-20 16:50:08 CST
