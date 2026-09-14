<h1 align="center">Kalmux</h1>

<p align="center">
<b>Mười con agent Claude Code chạy trong tmux, một chỗ để coi hết.</b><br> Một lớp quản lý mỏng nằm ngay trong iTerm2, không thay thế terminal của bạn.
</p>

<p align="center">
<img alt="macOS" src="https://img.shields.io/badge/macOS-13%2B-111?logo=apple&logoColor=white">
<img alt="iTerm2" src="https://img.shields.io/badge/iTerm2-3.7%2B-1f6feb">
<img alt="tmux" src="https://img.shields.io/badge/tmux-3.x-1bb91f">
<img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white">
<img alt="License" src="https://img.shields.io/badge/License-MIT-black">
</p>

<p align="center"><img src="assets/toolbelt.gif" alt="Toolbelt Kalmux: một agent chuyển vàng và nhảy lên đầu danh sách, bấm một cái là qua đúng tab, mấy session đã chết vẫn còn đó để resume" width="420"></p>

<p align="center"><sub>Một agent dừng lại hỏi, thẻ của nó chuyển vàng và nhảy lên đầu; bấm một cái là qua đúng tab. Quay từ mock server có sẵn trong repo.</sub></p>

<p align="center"><a href="README.md">English</a></p>

---

## Vì sao mình viết Kalmux

Một ngày làm việc ở [Kalera AI](https://www.kalera.ai) là bốn tới mười session tmux, mỗi session một con agent Claude Code, mỗi con một repo. Agent thì nhanh. Người mới là chỗ nghẽn: con này đang chờ mình trả lời, con kia cày hai chục phút chưa xong, con nọ làm xong từ mười phút trước mà không ai hay, còn một con sắp cạn context.

iTerm2 3.7 có sẵn tích hợp cho Claude Code, đúng thứ để trả lời mấy câu đó, mà nó hư khi chạy trong tmux. Mọi pane trong một tmux server đều thừa hưởng chung một `ITERM_SESSION_ID`, nên con agent nào cũng báo trạng thái về đúng một tab gateway ẩn. Session Status với Cockpit thì trống trơn trong khi mười con agent đang cày phía sau.

Kalmux sửa chỗ định tuyến đó rồi dựng luôn cái bảng điều khiển còn thiếu. Không thay thế thứ gì hết:

| Lớp             | Vẫn nguyên vai trò                                                          |
| --------------- | --------------------------------------------------------------------------- |
| **tmux**        | là server, nên SSH (mình dùng Tailscale) vẫn vô được từ máy khác            |
| **iTerm2**      | là máy khách ở bàn: control mode `tmux -CC`, tab thiệt, Cockpit, toolbelt   |
| **Claude Code** | không đụng tới; Kalmux chỉ đọc hook, session registry và status line của nó |

Ba mảnh nhỏ làm hết việc: một cái hook wrapper, CLI `kalmux`, và một trang web chạy ở `127.0.0.1:47321` cho iTerm2 hiện trong toolbelt.

## Được gì

**Mỗi session một thẻ, sắp theo ai cần mình trước.** Waiting lên đầu, rồi tới working, idle, busy. Bấm vô thẻ là nhảy qua tab iTerm2 của nó; session nào chưa có tab thì mở liền một tab trong cửa sổ đang làm.

**Vạch context trên từng thẻ.** Phần trăm lấy thẳng từ payload status line của Claude Code, nên nhìn là biết con nào sắp compact trước khi giao cho nó một prompt dài. Thẻ còn ghi model với chi phí của session, còn phần đầu trang hiện đồng hồ quota năm tiếng nếu gói của bạn có báo.

**Mục `Gone` kéo session sống dậy.** Ngày 14/09/2026, một con agent dọn sandbox của nó đã chạy `tmux kill-server`. Phiên nó chạy lại nằm ngay trong tmux, nên `$TMUX` trỏ vô server thiệt: bốn session, toàn bộ tab, và mấy tiếng làm việc bay sạch trong một câu lệnh. Giờ Kalmux ghi một vệt nhỏ cho từng session Claude (id, thư mục, pane tmux, màu nhận diện). Session nào chết thì thẻ của nó rớt xuống mục `Gone` kèm nút **Resume**: dựng lại session tmux đúng thư mục, đúng màu, gõ sẵn `claude --resume <id>` rồi chờ bạn bấm Enter. Repo cũng có file `CLAUDE.md` cấm tiệt agent chạy `tmux kill-server` lần nữa.

**Màu nhận diện sống dai.** Hai chục tên màu có sẵn hoặc mã hex tùy ý, lưu ngay trong session tmux và phát lại vô tab iTerm2 mỗi lần attach, nên một project giữ nguyên màu qua detach, qua reboot, qua cả lần dựng lại.

**Làm hết bằng bàn phím.** `Go to waiting` nhảy tới con agent đầu tiên đang chờ, `n` chạy vòng qua mấy con còn lại, `1`-`9` mở thẻ thứ n, `/` tìm theo tên session, tên project hoặc cái tiêu đề Claude tự đặt cho cuộc hội thoại.

**Xài được qua SSH.** `ssh mac kalmux ls` in ra đúng cái bảng đó mà không cần iTerm2, còn status line của tmux hiện `[working]` / `[waiting]` cho từng window khi attach kiểu thường.

## Cài

Cần macOS với iTerm2 3.7 trở lên, tmux 3.x, Python 3.11 trở lên, `jq`, và `uv` để đăng ký toolbelt.

```bash
brew install kalera-labs/tap/kalmux
kalmux setup
```

Homebrew đời mới hỏi mình có tin cái tap của bên thứ ba không rồi mới chịu chạy formula: `brew trust kalera-labs/tap`.

Hoặc clone về, cách này cũng là cách để sửa code:

```bash
git clone https://github.com/kalera-labs/kalmux.git
cd kalmux
bin/kalmux setup
```

Kiểu nào thì `kalmux` cũng nằm sẵn trong PATH: Homebrew tự đặt lệnh vô đó, còn clone thì `setup` trỏ `~/.local/bin/kalmux` về `bin/kalmux`.

`setup` chạy lại bao nhiêu lần cũng được và gỡ ra được. Nó tạo `~/.config/kalmux/config.toml`, link hook với CLI, chỉnh hai tùy chọn của iTerm2, thêm một khối được quản lý vô `~/.tmux.conf`, cài script AutoLaunch cho iTerm2 khởi động server UI, cho status line của Claude Code đi qua Kalmux, đăng ký tool trong toolbelt, rồi chạy `kalmux doctor` khép lại.

Xong thì mở toolbelt: **View > Toolbelt** (⇧⌘B) rồi chọn **Kalmux**. Attach lại mấy session một lần (`kalmux open <tên>` cho từng session, từ chung một cửa sổ) để tùy chọn tab mới có hiệu lực.

<details>
<summary>Setup đụng tới đâu, và vì sao</summary>

- `~/.config/iterm2/cc-status` → symlink tới hook wrapper. Đây là đường dẫn iTerm2 ghi vô `~/.claude/settings.json`. Cài lại tích hợp Claude Code của iTerm2 có thể làm mất link này; `kalmux doctor` phát hiện ra, `kalmux setup` gắn lại.
- `~/.local/bin/kalmux` → chính CLI, khi chạy từ bản clone. `kmux` với `tm` vẫn còn làm alias trỏ cùng chỗ, nên tay quen gõ kiểu cũ hoặc script cũ vẫn chạy. Cài bằng gói thì cái tên đó đã có chủ, `setup` để yên không đụng.
- Tùy chọn iTerm2 `OpenTmuxWindowsIn=2` (window tmux mở thành tab trong cửa sổ đang attach) và `AutoHideTmuxClientSession=true`.
- Một khối được quản lý trong `~/.tmux.conf`: `allow-passthrough on`, status line hiện `[working]` / `[waiting]` cho từng window, và một hook `client-attached` phát lại trạng thái với màu vô tab mới.
- `~/Library/Application Support/iTerm2/Scripts/AutoLaunch.scpt`, khởi động server UI mỗi lần iTerm2 mở. Script AutoLaunch do bạn tự viết thì không bao giờ bị ghi đè. Script sinh ra có nhúng sẵn đường dẫn tuyệt đối của trình thông dịch và một tiền tố `PATH`, tại iTerm2 khởi động với `PATH` của login shell, chỗ đó không thấy `python3` đời mới lẫn `tmux` của Homebrew, mà server không tìm ra `tmux` thì toolbelt trống trơn sau mỗi lần khởi động máy.
- `~/.claude/settings.json` → khóa `statusLine` được cho chạy qua `kalmux statusline`; giá trị cũ được lưu nguyên vẹn và nguyên file được chép ra `settings.json.bak-kalmux` (quyền 0600, vì file đó có thể chứa API key) trước khi sửa. Không muốn thì thêm `--no-statusline`.
- Tool trong toolbelt, đăng ký qua Python API của iTerm2. Cookie API lấy bằng AppleScript nên không hiện hộp thoại xin quyền nào hết.

**Vì sao để iTerm2 khởi động server chớ không phải launchd:** nói chuyện với iTerm2 cần một Apple Event, mà repo có thể nằm trên ổ ngoài. Tiến trình do launchd đẻ ra không có quyền TCC nào (Automation, Removable Volumes) và bị kẹt ở hộp thoại xin quyền của macOS, còn thứ gì sinh ra từ cây tiến trình của iTerm2 thì thừa hưởng đủ cả hai.

</details>

## Bộ lệnh

```
kalmux ls [--json]            mọi session kèm project, trạng thái Claude, % context, tuổi, tiêu đề, màu
kalmux go <chủ đề>            nhảy tới session theo tên, hoặc một phần tên project / tiêu đề Claude
kalmux open <session>         mở session thành tab control-mode mới trong cửa sổ hiện tại
kalmux color <session> <c>    màu nhận diện: #rrggbb, một trong 20 tên màu, hoặc none
kalmux new <tên> [--cwd D] [--color C] [--claude] [--no-attach]
kalmux kill | detach <session>        kalmux rename <session> <tên mới>
kalmux dead [--all] [--json]  mấy session Claude đã mất: bị giết trước, rồi tới mấy con thoát êm
kalmux resume <id|tên>        dựng lại session tmux và gõ sẵn `claude --resume <id>`
kalmux forget <session-id>    bỏ vệt của một session đã chết
kalmux attach <session>       đúng cách cho từng chỗ: -CC trong iTerm2, switch-client trong tmux, attach thường qua SSH
kalmux reapply [session]      gởi lại trạng thái với màu tab cho mấy pane đang attach
kalmux ui show|status|start|stop|restart|install|uninstall|url|serve
kalmux config                 coi cấu hình đang có hiệu lực và nó nằm ở đâu
kalmux statusline [install|uninstall|status]
kalmux doctor [--no-ui] [--no-statusline]
kalmux setup  [--no-ui] [--no-statusline]
```

## Kalmux biết bằng cách nào

Không đọc lén màn hình. Con số nào cũng có nguồn.

| Tín hiệu                         | Lấy từ đâu                                                                                                      |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `waiting` / `working` / `idle`   | hook của Claude Code, wrapper ghi vô pane option của tmux (`@cc_state`, `@cc_detail`, `@cc_since`)              |
| danh tính và còn sống hay không  | session registry của Claude Code, `~/.claude/sessions/<pid>.json`, ánh xạ mỗi hội thoại tới đúng một pane tmux  |
| `busy`                           | chính `#{pane_current_command}` của tmux: pane không có Claude mà đang chạy `make`, `pytest`, một script nào đó |
| % context, model, chi phí, quota | payload status line của Claude Code, do `kalmux statusline` hứng lại                                            |
| session đã mất                   | mấy file vệt Kalmux ghi thêm lúc `SessionStart`, `Stop` và `SessionEnd`                                         |
| danh tính tab                    | biến session `tmuxWindowPane` của iTerm2, nhờ vậy một pane tmux ứng với đúng một tab                            |

Hai trạng thái nói về sự cố chớ không phải công việc: `gone` là tiến trình Claude chết rồi, `stale` là trạng thái hook còn sót trên một pane không còn chạy Claude. Trong `kalmux ls`, dấu `~` là giá trị nhớ lại từ registry, dấu `?` là session đã working hoặc waiting hơn ba chục phút.

Session đã chết được xếp thành `killed` (chưa từng có `SessionEnd`), `clean` (thoát hoặc logout) và `superseded` (do `/clear` hoặc do resume, ẩn đi trừ khi thêm `--all`). Vệt với file trạng thái cũ hơn `tombstones.keep_days` được dọn tự động.

**Điểm mù đã biết:** một shell chạy script cùng loại với nó, kiểu script bash dưới shell bash, sẽ báo tên của chính cái shell đó, nên `busy` đọc ra thành dấu nhắc trống. Bắt được ca này phải cắm hook vô shell chớ format của tmux không thấy.

## Cấu hình

`~/.config/kalmux/config.toml` là của bạn; Kalmux tạo một lần rồi không bao giờ ghi đè.

```toml
[claude]
# Gõ vô session mới khi chạy `kalmux new --claude`.
new = "claude"
# Gõ vô pane khi chạy `kalmux resume`. {session_id} được thay bằng id session Claude.
resume = "claude --resume {session_id}"
# "type" để nguyên câu lệnh ở dấu nhắc; "run" bấm Enter luôn giùm bạn.
resume_mode = "type"

[tombstones]
keep_days = 30

[notify]
# "auto": đăng giùm cái thông báo iTerm2 mà Claude Code bỏ qua khi ở trong tmux, trừ khi bạn đã tự đặt preferredNotifChannel.
iterm2 = "auto"
bell = false
```

Ai quen xài `--dangerously-skip-permissions` thì bỏ vô hai mẫu lệnh này một lần, `new` với `resume` tự theo. Sửa xong có hiệu lực liền, khỏi khởi động lại.

**Thông báo.** Claude Code chọn kênh báo theo `TERM_PROGRAM`. tmux đặt biến đó thành `tmux`, nên trong tmux kênh mặc định `auto` không tìm ra cách nào và Claude im luôn, trong khi cũng Claude đó ở tab iTerm2 thường thì đăng thông báo macOS, có tiếng, ngay lúc nó cần mình. Cái im lặng này là thứ đầu tiên người ta để ý sau khi dọn hết agent vô tmux. Kalmux bù chỗ đó: đúng event `Notification` ấy, hook đăng cùng một cảnh báo OSC 9 vô pane, iTerm2 hiện y như cũ. `iterm2 = "auto"` tự nhường ngay khi `~/.claude.json` có `preferredNotifChannel` của riêng bạn (`claude config set -g preferredNotifChannel iterm2` cũng chạy được trong tmux, vì Claude tự bọc chuỗi cho tmux), `"always"` thì đăng bất kể, `"never"` tắt hẳn, còn `bell = true` thêm chuông terminal, giống `iterm2_with_bell` của Claude.

**Thư mục trạng thái:** vệt, bản ghi status và status line đã lưu đều nằm dưới `$KALMUX_STATE_DIR`, mặc định `~/.local/state/kalmux` (Kalmux không đọc `XDG_STATE_HOME`). Nếu bạn đổi biến này thì đặt ở chỗ mà Claude Code, hook và server UI đều thừa hưởng, vì file ghi theo giá trị này thì bên kia đọc bằng giá trị khác sẽ không thấy.

## Cái tap status line

`kalmux statusline` đứng trước status line bạn đang xài. Nó lưu một bản chuẩn hóa của từng lần refresh, chỗ mà vạch context đọc ra, rồi chuyền tối đa 1 MiB payload cho lệnh gốc của bạn và thoát với đúng mã thoát của lệnh đó, nên dấu nhắc của bạn vẫn hiện y như cũ. Tốn thêm khoảng 18 ms mỗi lần refresh.

Đối tượng `statusLine` gốc được lưu nguyên vẹn. `kalmux statusline uninstall` trả nó về chỗ cũ rồi đổi tên bản lưu chớ không xóa, nên lần cài sau nó ghi nhận đúng cái status line bạn đang xài lúc đó. Nếu bản lưu biến mất mà khóa vẫn đang đi qua Kalmux thì lệnh gỡ sẽ từ chối, không để bạn trơ trọi không còn status line nào, và `kalmux doctor` báo đỏ, chỉ thẳng tới file backup settings.

## An toàn

Server chỉ nghe trên loopback. Mọi lệnh gọi API đều mang một CSRF token riêng của tiến trình, đúc vô trang; `Host` với `Origin` bị ghim về loopback; trang chạy dưới CSP có nonce, không handler inline, không tài nguyên bên ngoài. Token này chống mấy trang web, chớ không chống được tiến trình khác cùng người dùng: thứ gì mở được socket loopback dưới danh nghĩa bạn thì cũng chạy thẳng `tmux kill-session` được.

Phần phát lại trạng thái chỉ ghi vô thiết bị ký tự dưới `/dev`, bản ghi tmux nào không giống id thiệt của tmux thì bị bỏ, và file backup settings ghi quyền 0600 vì trong đó có thể có API key.

## Trục trặc thì coi đâu

`kalmux doctor` kiểm file cấu hình, symlink hook, phần nối hook trong `settings.json`, `jq`, khối trong tmux.conf, hai tùy chọn iTerm2, đăng ký toolbelt, script AutoLaunch với mấy đường dẫn nhúng trong đó, đường đi của status line, và server: có trả lời không, có đang chạy đúng phiên bản code này không, và nó thấy `tmux` ở đâu.

Sau khi sửa code của Kalmux thì chạy `kalmux ui restart` từ một shell bên trong iTerm2, để server giữ được mấy quyền TCC của iTerm2.

## Phát triển

```bash
uv run --no-project --with pytest --with pytest-cov python -m pytest -q --cov=src/kalmux --cov-report=term-missing
uvx ruff check .
python3 scripts/dev/mock_server.py 47399    # giao diện với dữ liệu giả ở http://127.0.0.1:47399/
kalmux ui restart                           # sau khi sửa code server, chạy từ shell trong iTerm2
```

Chỉ xài thư viện chuẩn của Python, không phụ thuộc gì lúc chạy. `src/kalmux/` chứa mấy module, `src/kalmux/assets/` chứa hook wrapper với toàn bộ giao diện gói trong một file vanilla JS, `bin/kalmux` cho phép chạy thẳng từ bản clone.

## Giấy phép

MIT. Làm bởi [Kalera AI](https://www.kalera.ai).
