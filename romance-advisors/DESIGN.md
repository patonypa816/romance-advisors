---
name: 恋爱军师团
description: 手机直播观众叠层与本地操作工作台的视觉约定
colors:
  overlay-ink: "#3b3030"
  overlay-paper: "#f7efe5"
  overlay-peach: "#f0c4af"
  overlay-berry: "#edb0bb"
  overlay-muted: "#d3bfb5"
  workbench-background: "#30241f"
  workbench-paper: "#fff5e7"
  workbench-red: "#a84d62"
  workbench-border: "#665044"
  workbench-focus: "#ffc9ae"
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
    backgroundColor: "{colors.overlay-peach}"
    textColor: "{colors.overlay-ink}"
    rounded: "{rounded.overlay-option}"
    padding: "8px 14px"
  workbench-button:
    backgroundColor: "#a94e63"
    textColor: "#fff7ee"
    rounded: "{rounded.workbench-control}"
    padding: "10px 15px"
---

# Design System: 恋爱军师团

## Overview

**Creative North Star: "咖啡馆恋爱轻喜剧"**

节目以咖啡馆恋爱轻喜剧为视觉方向。横标用奶油字、杏桃字和深可可描边，折扇与冒汗爱心延续求助的笑点。聊天、候选与榜单采用暖墨底、奶油纸和蜜桃强调色，狗头榜用柔莓色。

工作台以深咖啡色为底，表单使用奶油纸色，主操作用莓红色。抖音手机预览和 OBS 共用节目背景、标题图片与游戏叠层。

**Key Characteristics:**
- 透明画布上使用独立、清晰的内容底板。
- 斜体中文榜单标题与系统中文正文分工。
- 选项、票数与选中状态有明确层次。
- 工作台延续温暖暗色操作界面。

## Colors

普通候选用奶油底和暖墨字。男主气泡、选中项和最强榜使用蜜桃色，狗头榜使用柔莓色。亲密度随实际分数变色。

## Typography

双榜标题使用本地得意黑 SmileySans（30px），其余为系统中文黑体。聊天正文40px/1.4，投票摘要38px/1.3，编号32px，票数28px。数字均为1080×1920源画布坐标；390宽预览中聊天约14.4px、投票约13.7px。阶段28px、人物姓名34px、分数56px，榜单昵称32px、成绩40px、采纳次数26px。

## Layout

观众画布保持1080×1920透明、等比缩放。满内容版保留左聊天、右3:4人物、下方两列两行候选；双榜移到人物下方，与聊天区底部齐平。默认内容边距上336、右48、下312、左48。

- 手机聊天：(48,336)，480×944，底部1280。
- 人物：(552,360)，480×640，底部1000。
- 双榜：(552,1024)，480×256，间隔12，各122高，底部1280。
- 共享投票：(48,1304)，984×304，底部1608。选项每项最多两行，2×2排列。

上下边距可调320–380和280–400；人物顶部跟随上边距至440，宽度约束保留其下24间隔、256高双榜以及共享投票区。主内容不超y1640。新的边界依据未拉伸截图中首条评论约y1696的位置调整；是本轮试排值，仍需用户目测和真实直播设备校准。顶部活动卡可覆盖人物头发附近，脸部须在画面中检查。

工作台在 9:16 画布内居中显示等高的 851×1848 抖音截图参考框。标题、游戏组件和人物共同等比缩放，节目背景铺满画布。定稿漫画标题使用 `static/assets/show-nameplate-cafe-natural.png`，以 495 源像素宽度等比显示，底边与聊天框之间至少留 14 源像素。完整排版示例只在工作台通过开关启用：使用同一renderer、批准男主静态原图、虚构长聊天/四候选/票数/双榜；不写入游戏记录。开始自动联调会切回实时。默认/overlay始终读取真实公开状态。

工作台大屏为左侧直播预览、右侧模拟评论与操作；主题、亲密度和剧情状态集中在预览中，不再重复展示。900px以下收紧间距，760px以下改为上下排列；推进设置、排版设置和性能记录折叠展示。辅助参考线和截图仅属于操作者预览。

## Elevation & Depth

观众叠层主要靠实色底板、颜色和留白区分层次，不使用阴影制造深度。工作台以边线、深浅底色及分区间距组织操作。

## Shapes

手机框采用40px圆角和细边框，屏幕内侧为20px圆角；投票行和榜单均12px。聊天气泡通过不对称圆角及左右位置区分发言方向。工作台控件使用 8px 圆角和细边框。

## Components

### 观众信息与投票

姓名、轮次和亲密度集中在手机顶部，分数条随亲密度变色。双方聊天持续保留并跟随最新消息；最新女生回复超过视口时，沿用每秒20源像素往返、端点停3秒的阅读滚动；减少动态效果时每4秒分段跳转。长内容不丢弃，完整聊天仍由游戏保存。

投票标题与2×2候选共用304高区域。摘要用38px两行均衡换行，编号和票数独立占位。超长摘要最多显示两行，完整内容保留title和工作台；采纳后展示实际选项及作者。摘要生成规则、票数和选项来源不因视觉改动而变化。

### 榜单

双榜在人物下方纵向排列，各展示首位。昵称、采纳次数在左，成绩在右纵向居中。长昵称截断并保留title，成绩不随昵称挤压。墨色底板统一两榜，蜜桃/柔莓分别表示最强/狗头；不过度抢占聊天与人物的注意力。

### 工作台控件

主按钮为暗红底，悬停变亮；普通按钮为透明底和细边框。输入框采用奶油底及暖色边框，键盘焦点使用 2px 焦点轮廓。工作台不另设完整聊天模块；游戏历史数据保留，聊天在直播预览内显示。

本地模拟评论位于直播预览右侧，切换稳定身份并发送统一评论，下方快捷投票也发送“投N”。评论流在同列回看原文与处理回执，推进设置及建议候选箱收于折叠区。主题、亲密度及剧情阶段只在预览内展示；窄屏按预览、操作顺序纵向排列。使用文字与颜色共同区分候选、已计票、仅记录和过期；候选不称为已入选。网络失败保留原文及事件ID供重试，轮询不覆盖未保存设置或抢走评论阅读位置。

## Do's and Don'ts

### Do:
- **Do** 保持观众画布透明，仅在内容组件上使用底板。
- **Do** 用安全文本节点呈现用户与模型文本，避免将内容作为 HTML 执行。
- **Do** 以实际手机画面检查平台遮挡和文字可读性。

### Don't:
- **Don't** 在观众画面铺满粉色泡泡。
- **Don't** 把预览中的参考线、遮挡示意或数字人辅助框放进透明输出。
