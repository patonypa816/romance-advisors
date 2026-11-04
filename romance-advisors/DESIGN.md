---
name: 恋爱军师团
description: 手机直播观众叠层与本地操作工作台的视觉约定
colors:
  overlay-ink: "#172626"
  overlay-paper: "#f6f7ed"
  overlay-lime: "#d8f36a"
  overlay-coral: "#ffb1a0"
  overlay-muted: "#b9c9c6"
  workbench-background: "#232324"
  workbench-paper: "#f5eee5"
  workbench-red: "#a95361"
  workbench-border: "#484345"
  workbench-focus: "#f2b6a7"
typography:
  overlay-display:
    fontFamily: "SmileySans, sans-serif"
    fontSize: "28px"
    fontWeight: 400
    lineHeight: 1
  overlay-body:
    fontFamily: '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'
    fontSize: "40px"
    lineHeight: 1.4
  overlay-option:
    fontFamily: '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'
    fontSize: "38px"
    lineHeight: 1.3
  workbench-body:
    fontFamily: '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
rounded:
  overlay-phone: "40px"
  overlay-screen: "20px"
  overlay-card: "12px"
  overlay-option: "12px"
  workbench-control: "8px"
spacing:
  overlay-gap: "12px"
  option-gap: "8px"
components:
  overlay-option:
    backgroundColor: "{colors.overlay-paper}"
    textColor: "{colors.overlay-ink}"
    rounded: "{rounded.overlay-option}"
    padding: "8px 14px"
  overlay-option-chosen:
    backgroundColor: "{colors.overlay-lime}"
    textColor: "{colors.overlay-ink}"
    rounded: "{rounded.overlay-option}"
    padding: "8px 14px"
  workbench-button:
    backgroundColor: "#a85361"
    textColor: "#fff7ee"
    rounded: "{rounded.workbench-control}"
    padding: "10px 15px"
---

# Design System: 恋爱军师团

## Overview

**Creative North Star: "城市音乐节节目单"**

观众叠层以深墨、柠檬和近白组织阶段状态、亲密度、来信与投票；组件各有底板，画布保持透明。珊瑚用于负向变化和狗头榜。设计服务于手机直播阅读，保留用户要求的弹幕避让，不采用满屏粉色泡泡。

操作员工作台保留既有的暖暗灰、暗红和宋体标题体系，负责操控与查看评论回执；其手机预览嵌入真实观众叠层。两种界面有不同用途，不应误认为工作台也已换成叠层配色。产品背景见 `docs/01-产品需求.md`。

**Key Characteristics:**
- 透明画布上使用独立、清晰的内容底板。
- 斜体中文榜单标题与系统中文正文分工。
- 选项、票数与选中状态有明确层次。
- 工作台延续温暖暗色操作界面。

## Colors

观众画面的墨色承载信息面板，近白承载普通选项与正文，柠檬色强调最强榜、亲密度和选中项；珊瑚用于负向反馈。柔和灰绿用于次要文字。工作台则使用暖暗灰背景、纸色文字与暗红操作按钮。

## Typography

双榜标题使用本地得意黑 SmileySans（30px），其余为系统中文黑体。聊天正文40px/1.4，投票摘要38px/1.3，编号32px，票数28px。数字均为1080×1920源画布坐标；390宽预览中聊天约14.4px、投票约13.7px。阶段28px、人物姓名34px、分数56px，榜单昵称32px、成绩40px、采纳次数26px。

## Layout

观众画布保持1080×1920透明、等比缩放。满内容版保留左聊天、右3:4人物、下方两列两行候选；双榜移到人物下方，与聊天区底部齐平。默认内容边距上336、右48、下312、左48。

- 手机聊天：(48,336)，480×944，底部1280。
- 人物：(552,360)，480×640，底部1000。
- 双榜：(552,1024)，480×256，间隔12，各122高，底部1280。
- 共享投票：(48,1304)，984×304，底部1608。选项每项最多两行，2×2排列。

上下边距可调320–380和280–400；人物顶部跟随上边距至440，宽度约束保留其下24间隔、256高双榜以及共享投票区。主内容不超y1640。新的边界依据未拉伸截图中首条评论约y1696的位置调整；是本轮试排值，仍需用户目测和真实直播设备校准。顶部活动卡可覆盖人物头发附近，脸部须在画面中检查。

工作台保留抖音截图自身的长屏比例，与9:16画布顶部和左右对齐；截图底部延伸部分不进入实际游戏画布。完整排版示例只在工作台通过开关启用：使用同一renderer、批准男主静态原图、虚构长聊天/四候选/票数/双榜；不写入游戏记录。开始自动联调会切回实时。默认/overlay始终读取真实公开状态。

工作台大屏为左侧直播预览、右侧模拟评论与操作；主题、亲密度和剧情状态集中在预览中，不再重复展示。900px以下收紧间距，760px以下改为上下排列；推进设置、排版设置和性能记录折叠展示。辅助参考线和截图仅属于操作者预览。

## Elevation & Depth

观众叠层主要靠实色底板、颜色和留白区分层次，不使用阴影制造深度。工作台以边线、深浅底色及分区间距组织操作。

## Shapes

手机框采用40px圆角和细边框，屏幕内侧为20px圆角；投票行和榜单均12px。聊天气泡通过不对称圆角及左右位置区分发言方向。工作台控件使用 8px 圆角和细边框。

## Components

### 观众信息与投票

姓名、轮次和亲密度集中在手机顶部，分数条使用柠檬色。双方聊天持续保留并跟随最新消息；最新女生回复超过视口时，沿用每秒20源像素往返、端点停3秒的阅读滚动；减少动态效果时每4秒分段跳转。长内容不丢弃，完整聊天仍由游戏保存。

投票标题与2×2候选共用304高区域。摘要用38px两行均衡换行，编号和票数独立占位。超长摘要最多显示两行，完整内容保留title和工作台；采纳后展示实际选项及作者。摘要生成规则、票数和选项来源不因视觉改动而变化。

### 榜单

双榜在人物下方纵向排列，各展示首位。昵称、采纳次数在左，成绩在右纵向居中。长昵称截断并保留title，成绩不随昵称挤压。墨色底板统一两榜，柠檬/珊瑚分别表示最强/狗头；不过度抢占聊天与人物的注意力。

### 工作台控件

主按钮为暗红底，悬停变亮；普通按钮为透明底和细边框。输入框采用深色底及边框，键盘焦点使用 2px 焦点轮廓。工作台不另设完整聊天模块；游戏历史数据保留，聊天在直播预览内显示。

本地模拟评论位于直播预览右侧，切换稳定身份并发送统一评论，下方快捷投票也发送“投N”。评论流在同列回看原文与处理回执，推进设置及建议候选箱收于折叠区。主题、亲密度及剧情阶段只在预览内展示；窄屏按预览、操作顺序纵向排列。使用文字与颜色共同区分候选、已计票、仅记录和过期；候选不称为已入选。网络失败保留原文及事件ID供重试，轮询不覆盖未保存设置或抢走评论阅读位置。

## Do's and Don'ts

### Do:
- **Do** 保持观众画布透明，仅在内容组件上使用底板。
- **Do** 用安全文本节点呈现用户与模型文本，避免将内容作为 HTML 执行。
- **Do** 以实际手机画面检查平台遮挡和文字可读性。

### Don't:
- **Don't** 在观众画面铺满粉色泡泡。
- **Don't** 把预览中的参考线、遮挡示意或数字人辅助框放进透明输出。
