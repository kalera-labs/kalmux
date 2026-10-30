<h1 align="center">Kalmux</h1>

<p align="center">
<b>Mười con agent Claude Code chạy trong tmux, một chỗ để coi hết.</b><br> Một lớp quản lý mỏng nằm ngay trong iTerm2, không thay thế terminal của bạn.
</p>

<p align="center">
<img alt="macOS" src="https://img.shields.io/badge/macOS-13%2B-111?logo=apple&logoColor=white">
<img alt="iTerm2" src="https://img.shields.io/badge/iTerm2-3.7.5%2B-1f6feb">
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

iTerm2 3.7 có sẵn tích hợp cho Claude Code, đúng thứ để trả lời mấy câu đó, mà từng tab một. Mấy bản đầu của nó hư khi chạy trong tmux: mọi pane trong một tmux server đều thừa hưởng chung một `ITERM_SESSION_ID`, nên con agent nào cũng báo trạng thái về đúng một tab gateway ẩn. Tới iTerm2 3.7.5 thì chỗ đó đã sửa, nhưng trạng thái nằm rải theo từng tab thì vẫn phải bấm qua mười cái tab mới kiếm ra con đang chờ mình, còn ngoài iTerm2 (shell SSH, thanh trạng thái tmux) thì chẳng thấy gì hết.

Kalmux sửa chỗ định tuyến đó rồi dựng luôn cái bảng điều khiển còn thiếu. Không thay thế thứ gì hết:

| Lớp             | Vẫn nguyên vai trò                                                          |
| --------------- | --------------------------------------------------------------------------- |
| **tmux**        | là server, nên SSH (mình dùng Tailscale) vẫn vô được từ máy khác            |
| **iTerm2**      | là máy khách ở bàn: control mode `tmux -CC`, tab thiệt, Cockpit, toolbelt   |
| **Claude Code** | không đụng tới; Kalmux chỉ đọc hook, session registry và status line của nó |

Ba mảnh nhỏ làm hết việc: một cái hook wrapper, CLI `kalmux`, và một trang web chạy ở `127.0.0.1:47321` cho iTerm2 hiện trong toolbelt.

## Được gì

**Mỗi session một thẻ, sắp theo ai cần mình trước.** Waiting lên đầu, rồi tới working, idle, busy. Bấm vô thẻ là nhảy qua tab iTerm2 của nó; session nào chưa có tab thì mở liền một tab trong cửa sổ đang làm.

**Vạch context trên từng thẻ.** Phần trăm lấy thẳng từ payload status line của Claude Code, nên nhìn là biết con nào sắp compact trước khi giao cho nó một prompt dài. Thẻ còn ghi model với chi phí của session, còn phần đầu trang hiện đồng hồ quota năm tiếng nếu gói của bạn có báo (chỉ lấy số đo chưa quá một tiếng, và không bao giờ lấy một khung giờ đã reset rồi).

**Mục `Gone` kéo session sống dậy.** Ngày 14/09/2026, một con agent dọn sandbox của nó đã chạy `tmux kill-server`. Phiên nó chạy lại nằm ngay trong tmux, nên `$TMUX` trỏ vô server thiệt: bốn session, toàn bộ tab, và mấy tiếng làm việc bay sạch trong một câu lệnh. Giờ Kalmux ghi một vệt nhỏ cho từng session Claude (id, thư mục, pane tmux, màu nhận diện). Session nào chết thì thẻ của nó rớt xuống mục `Gone` kèm nút **Resume**: dựng lại session tmux đúng thư mục, đúng màu, gõ sẵn `claude --resume <id>` rồi chờ bạn bấm Enter. Repo cũng có file `CLAUDE.md` cấm tiệt agent chạy `tmux kill-server` lần nữa.

**Reboot hay crash gì session cũng trở về.** Reboot một cái là server tmux chết, kéo theo mọi session; iTerm2 crash hay bị force quit cũng vậy: iTerm2 chạy server Kalmux, server Kalmux chạy tmux, macOS dẹp nguyên cụm đó một lượt. Kalmux ghi danh sách đang sống mỗi năm giây kèm luôn lý lịch của cái server tmux đẻ ra danh sách đó, nên hễ có một server khác trả lời (hoặc chẳng còn server nào) là nó dựng lại từng session còn mở lúc đó: y tên, y thư mục, y màu. Có điều nó không bung ra mỗi con một tab: toolbelt hiện một dải báo có nút **Open all**, thẻ nào vừa dựng lại thì có nhãn `restored`, bạn muốn mở con nào thì bấm con đó, hay mở hết một lượt cũng được. Vô session vừa dựng gõ `claude --continue` là nối lại đúng cuộc hội thoại của thư mục đó liền.

**Màu nhận diện sống dai.** Hai chục tên màu có sẵn hoặc mã hex tùy ý, lưu ngay trong session tmux và phát lại vô tab iTerm2 mỗi lần attach, nên một project giữ nguyên màu qua detach, qua reboot, qua cả lần dựng lại.

**Làm hết bằng bàn phím.** `Go to waiting` nhảy tới con agent đầu tiên đang chờ, `n` chạy vòng qua mấy con còn lại, `1`-`9` mở thẻ thứ n, `/` tìm theo tên session, tên project hoặc cái tiêu đề Claude tự đặt cho cuộc hội thoại.

**Xài được qua SSH.** `ssh mac kalmux ls` in ra đúng cái bảng đó mà không cần iTerm2, còn status line của tmux hiện `[working]` / `[waiting]` cho từng window khi attach kiểu thường.

Lệnh SSH không tương tác chỉ có `PATH` trơn: không có `~/.local/bin`, không có Homebrew, còn `python3` thì là bản 3.9 của macOS. Nếu `ssh mac kalmux ls` báo `command not found`, gọi bằng đường dẫn đầy đủ (`ssh mac '~/.local/bin/kalmux ls'`) hoặc thêm `export PATH="$HOME/.local/bin:$PATH"` vô `~/.zshenv`, file mà zsh đọc cho mọi lần chạy, có tương tác hay không. `bin/kalmux` trong bản clone tự xử được cái `python3` cũ đó: nó chạy lại chính nó bằng một Python 3.11+ tìm thấy trong Homebrew, python.org, uv hoặc `~/.local/bin`, hay bằng cái mà `KALMUX_PYTHON=/path/to/python3.13` chỉ định, và nếu không có cái nào thì in đúng một dòng nói cần cài gì.

## Cài

Cần macOS với iTerm2 3.7.5 trở lên (3.7.0 tới 3.7.4 vẫn chạy, có điều hook Claude Code riêng của mấy bản đó báo mọi pane tmux về một tab ẩn, và `kalmux doctor` sẽ nói cho bạn biết), tmux 3.x, Python 3.11 trở lên, `jq`, và `uv` để đăng ký toolbelt.

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

`setup` chạy lại bao nhiêu lần cũng được và gỡ ra được. Nó tạo `~/.config/kalmux/config.toml`, thêm các entry hook riêng của Kalmux vô `~/.claude/settings.json`, link CLI, chỉnh hai tùy chọn của iTerm2, thêm một khối được quản lý vô `~/.tmux.conf`, cài script AutoLaunch cho iTerm2 khởi động server UI, cho status line của Claude Code đi qua Kalmux, đăng ký tool trong toolbelt, rồi chạy `kalmux doctor` khép lại.

Xong thì mở toolbelt: **View > Toolbelt** (⇧⌘B) rồi chọn **Kalmux**. Attach lại mấy session một lần (`kalmux open <tên>` cho từng session, từ chung một cửa sổ) để tùy chọn tab mới có hiệu lực.

<details>
<summary>Setup đụng tới đâu, và vì sao</summary>

- `~/.claude/settings.json` → `hooks`: mỗi event mà wrapper xử lý có một entry của Kalmux (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`, `SessionEnd`), chạy `kalmux-hook` với timeout 5 giây. Mấy entry `cc-status` của iTerm2 để nguyên y vậy, chạy song song. Lệnh không bao giờ chứa chữ `cc-status`, vì iTerm2 coi entry nào có chữ đó là của nó (nó viết lại và gộp bớt), và trỏ tới đường dẫn còn sống qua mỗi lần nâng cấp (`/opt/homebrew/opt/kalmux/...` khi cài bằng Homebrew, không bao giờ là thư mục Cellar có số phiên bản). Lần đầu file bị đổi, nó được chép ra `settings.json.hooks.bak-kalmux` (mode 0600). Khi iTerm2 3.7.5 trở lên đã có hook riêng, các entry mang `--status=iterm2`: lúc đó iTerm2 tự báo trạng thái Claude cho pane tmux, wrapper chỉ giữ phần việc của Kalmux (trạng thái tmux, vệt, màu tab, cảnh báo), nên không bao giờ có hai bên cùng ghi.
- `~/.config/iterm2/cc-status` là của iTerm2. Kalmux tới bản 0.5.1 thay nó bằng symlink tới wrapper; mỗi lần một bản iTerm2 mới hơn khởi động, iTerm2 gắn lại link của nó, vậy là Kalmux bị cắt mà không ai hay. Giờ `setup` trả đường dẫn đó về cho iTerm2 (trỏ lại `cc-status` trong bundle) và ngoài ra không đụng tới nó nữa.
- `~/.local/bin/kalmux` → chính CLI, khi chạy từ bản clone. `kmux` với `tm` vẫn còn làm alias trỏ cùng chỗ, nên tay quen gõ kiểu cũ hoặc script cũ vẫn chạy. Cài bằng gói thì cái tên đó đã có chủ, `setup` để yên không đụng.
- Tùy chọn iTerm2 `OpenTmuxWindowsIn=2` (window tmux mở thành tab trong cửa sổ đang attach) và `AutoHideTmuxClientSession=true`.
- Một khối được quản lý trong `~/.tmux.conf`: `allow-passthrough on`, `exit-empty off` (giết session cuối cùng thì server vẫn còn, khỏi bị nhìn nhầm thành một cú crash), status line hiện `[working]` / `[waiting]` cho từng window, và một hook `client-attached` phát lại trạng thái với màu vô tab mới.
- `~/Library/Application Support/iTerm2/Scripts/AutoLaunch.scpt`, khởi động server UI mỗi lần iTerm2 mở. Script AutoLaunch do bạn tự viết thì không bao giờ bị ghi đè. Script sinh ra có nhúng sẵn đường dẫn tuyệt đối của trình thông dịch và một tiền tố `PATH`, tại iTerm2 khởi động với `PATH` của login shell, chỗ đó không thấy `python3` đời mới lẫn `tmux` của Homebrew, mà server không tìm ra `tmux` thì toolbelt trống trơn sau mỗi lần khởi động máy.
- `~/.claude/settings.json` → khóa `statusLine` được cho chạy qua `kalmux statusline`; giá trị cũ được lưu nguyên vẹn và nguyên file được chép ra `settings.json.bak-kalmux` (quyền 0600, vì file đó có thể chứa API key) trước khi sửa. Không muốn thì thêm `--no-statusline`. Lệnh được ghi vô là `<python> <kalmux> statusline` với cả hai đường dẫn tuyệt đối (interpreter đã chạy `setup`), nên nó không phụ thuộc `PATH` mà Claude Code chạy nó, và status line của bạn không bị trống chỉ vì `python3` trúng bản quá cũ. Chạy lại `kalmux setup` sẽ nâng lên dạng này cho lệnh trơn kiểu 0.5, hay lệnh có interpreter đã mất hoặc cũ hơn 3.11; còn interpreter đã nướng vô mà vẫn chạy tốt thì để nguyên, nên chạy setup bằng một `python3` khác (từ shell SSH chẳng hạn) cũng không viết lại `settings.json` mỗi lần. Chỉ lần đầu cho chạy qua Kalmux mới chép `settings.json.bak-kalmux`: nướng lại interpreter không bao giờ đè bản sao của bạn bằng một bản đã có sẵn lệnh của Kalmux. `kalmux doctor` báo đỏ nếu interpreter đã nướng vô bị xóa mất.
- Tool trong toolbelt, đăng ký qua Python API của iTerm2. Cookie API lấy bằng AppleScript nên không hiện hộp thoại xin quyền nào hết.

**Vì sao để iTerm2 khởi động server chớ không phải launchd:** nói chuyện với iTerm2 cần một Apple Event, mà repo có thể nằm trên ổ ngoài. Tiến trình do launchd đẻ ra không có quyền TCC nào (Automation, Removable Volumes) và bị kẹt ở hộp thoại xin quyền của macOS, còn thứ gì sinh ra từ cây tiến trình của iTerm2 thì thừa hưởng đủ cả hai.

</details>

## Bộ lệnh

```
kalmux ls [--json]            mọi session kèm project, trạng thái Claude, % context, tuổi, tiêu đề, màu
kalmux go <chủ đề>            nhảy tới session theo tên, hoặc một phần tên project / tiêu đề Claude; qua SSH thì attach tại chỗ
kalmux open <session>         mở session thành tab control-mode mới trong cửa sổ hiện tại (session mất là tab đóng theo)
kalmux open --restored        mở hết mấy session vừa được dựng lại mà chưa ai mở, chung một cửa sổ
kalmux color <session> <c>    màu nhận diện: #rrggbb, một trong 20 tên màu, hoặc none
kalmux new <tên> [--cwd D] [--color C] [--claude] [--no-attach]
kalmux kill | detach <session>        kalmux rename <session> <tên mới>
kalmux dead [--all] [--json]  mấy session Claude đã mất: bị giết trước, rồi tới mấy con thoát êm
kalmux resume <id|tên>        dựng lại session tmux và gõ sẵn `claude --resume <id>`
kalmux forget <session-id>    bỏ vệt của một session đã chết
kalmux restore [--dry-run] [--tabs | --no-tabs]
                              dựng lại mấy session còn mở trước lúc server tmux đi mất
kalmux attach <session>       đúng cách cho từng chỗ: -CC trong iTerm2, switch-client trong tmux, attach thường qua SSH
kalmux reapply [session]      gởi lại trạng thái với màu tab cho mấy pane đang attach
kalmux ui show|status|start|stop|restart|install|uninstall|url|serve
kalmux config                 coi cấu hình đang có hiệu lực và nó nằm ở đâu
kalmux statusline [install|uninstall|status]
kalmux doctor [--no-ui] [--no-statusline] [--fix]
kalmux setup  [--no-ui] [--no-statusline]
```

## Kalmux biết bằng cách nào

Không đọc lén màn hình. Con số nào cũng có nguồn.

| Tín hiệu                         | Lấy từ đâu                                                                                                      |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `waiting` / `working` / `idle`   | hook của Claude Code (entry riêng của Kalmux), wrapper ghi vô pane option của tmux (`@cc_state`, `@cc_detail`, `@cc_since`) |
| danh tính và còn sống hay không  | session registry của Claude Code, `~/.claude/sessions/<pid>.json`, ánh xạ mỗi hội thoại tới đúng một pane tmux  |
| `busy`                           | chính `#{pane_current_command}` của tmux: pane không có Claude mà đang chạy `make`, `pytest`, một script nào đó |
| % context, model, chi phí, quota | payload status line của Claude Code, do `kalmux statusline` hứng lại                                            |
| session đã mất                   | mấy file vệt Kalmux ghi thêm lúc `SessionStart`, `Stop` và `SessionEnd`                                         |
| danh tính tab                    | biến session `tmuxWindowPane` của iTerm2, nhờ vậy một pane tmux ứng với đúng một tab                            |

Hai trạng thái nói về sự cố chớ không phải công việc: `gone` là tiến trình Claude chết rồi, `stale` là trạng thái hook còn sót trên một pane không còn chạy Claude. Trong `kalmux ls`, dấu `~` là giá trị nhớ lại từ registry, dấu `?` (trong API là cờ `overdue`, có thêm `counts.overdue`) là session đang working hoặc waiting mà im re hơn ba chục phút: không đổi trạng thái, không làm tươi status line, không ghi transcript. Tuổi trên card là thời gian ở trạng thái hiện tại, nên một lượt chạy dài hay workflow nền có thể hiện `working 8h` mà không có `?` miễn là nó còn ghi. Thanh context, model và chi phí còn nguyên trên card chừng nào registry nói session còn sống (tooltip cho biết số liệu cũ cỡ nào, cũ quá thì mờ đi); chỉ biến mất sau một giờ kể từ lúc session mất. Trạng thái `waiting` trong registry hiện đúng là `waiting`, kèm lý do của Claude (`permission prompt`, `input needed`, `dialog open`) làm chi tiết trên card.

Session đã chết được xếp thành `killed` (chưa từng có `SessionEnd`), `clean` (thoát hoặc logout) và `superseded` (do `/clear` hoặc do resume, ẩn đi trừ khi thêm `--all`). Vệt với file trạng thái cũ hơn `tombstones.keep_days` được dọn tự động.

**Điểm mù đã biết:** một shell chạy script cùng loại với nó, kiểu script bash dưới shell bash, sẽ báo tên của chính cái shell đó, nên `busy` đọc ra thành dấu nhắc trống. Bắt được ca này phải cắm hook vô shell chớ format của tmux không thấy.

## Dựng lại session sau khi reboot hay sau một cú crash

Reboot là server tmux chết, mọi session chết theo; iTerm2 crash hay bị force quit cũng y vậy: AutoLaunch chạy server Kalmux, server Kalmux chạy tmux, ba đứa nằm chung một coalition tài nguyên của macOS nên đi là đi chung. Bữa 4/10/2026 ở đây dính đúng ca đó: không reboot, không đăng xuất, iTerm2 tắt chừng nửa giây là session cuối cùng đi theo. Thoát iTerm2 bình thường thì không sao: nó chỉ detach mấy client control-mode thôi. Giết một session cũng không sao: đó là bạn tự tay quyết. Vậy nên thứ Kalmux ghi xuống không phải "session mất rồi" mà là danh sách đã lưu thuộc về server tmux nào.

Server UI ghi danh sách session đang sống (tên, thư mục, màu nhận diện, thứ tự tạo) vô `${KALMUX_STATE_DIR}/sessions.json` mỗi năm giây, ghi liền sau mỗi lần kill, rename, new hay resume, kèm luôn pid với giờ khởi động của cái server tmux đang giữ mấy session đó. Có gì đổi nó mới ghi, mà hễ không server nào trả lời thì nó không ghi gì hết: server đi mất là server mà bạn đâu có giết session của nó. Lúc được kêu dừng, nếu tmux còn trả lời được thì nó chụp thêm một bản cuối.

`kalmux setup` bỏ dòng `set -s exit-empty off` vô khối quản lý trong `~/.tmux.conf`, nên giết session cuối cùng thì server vẫn đứng đó với danh sách rỗng chớ không tắt. Thiếu dòng đó là hai ca y hệt nhau (giết con cuối, với tmux chết), và con bạn vừa giết sẽ bò về ở lần dựng lại kế tiếp. `kalmux doctor` có kiểm dòng này.

Tới cữ dựng lại khi danh sách đã lưu còn session mà cái server viết ra nó không phải server đang trả lời bây giờ: hoặc chẳng có gì chạy (reboot, hoặc tmux chết mà không ai mở lại), hoặc một server khác đang ôm cái socket. Chỗ kiểm là hai: lúc server UI khởi động (lo ca reboot và ca crash kéo luôn Kalmux theo iTerm2), và ở mỗi vòng năm giây, lo ca tmux chết mà server Kalmux còn sống nhăn. Đằng nào thì danh sách đã lưu cũng được chép qua `sessions.previous.json` trước, rồi dựng lại mọi session chưa có (tên nào đang có chủ thì để yên, không giành, không đổi tên), rồi ghi lại trạng thái của server đang chạy, để không dựng lại lần thứ hai. Nó làm gì đều nằm trong `ui.log`, còn `kalmux doctor` cho coi bản ghi cũ bao lâu rồi, còn khớp với mấy session đang sống hay không, và có còn thuộc về một server đã đi mất hay không.

**Bạn chưa kêu thì chưa mở tab.** Mặc định (`open_tabs = false` trong `[restore]`) dựng lại xong là thôi, không đụng gì tới iTerm2: mới đăng nhập mà hai chục cái tab bung ra một lượt thì chẳng ai ưng. Session nào được dựng lại thì mang một dấu trong tmux (session option `@kalmux_restored`, giá trị là giờ dựng lại), nên server UI có khởi động lại thì dấu vẫn còn. Toolbelt hiện "N sessions restored at HH:MM, not opened yet" kèm hai nút **Open all** với **Dismiss**, và mấy thẻ đó có nhãn `restored`. Bấm vô thẻ là mở tab như thường lệ, dấu của nó cũng mất luôn; **Open all** mở mỗi session có dấu một tab trong cửa sổ iTerm2 hiện tại, chạy trên một thread riêng, tiến độ hiện ngay trên dải báo; **Dismiss** giữ nguyên session, chỉ bỏ dấu. Thẻ nào bạn bấm (hay session nào bạn dismiss) trong lúc **Open all** còn đang chạy thì nó bỏ qua, nên không session nào bị mở hai tab, còn tab nào mở không được thì dải báo đếm lại ("2 of 8 could not be opened (see ui.log)") cho tới lần **Open all** kế. Session nào được attach bằng đường khác (`tmux attach`, ssh) thì trong vòng năm giây là mất dấu. Ở terminal thì `kalmux open --restored` làm y như **Open all**, còn `ui.log` ghi "recreated, not opened" cho từng con. Muốn quay lại kiểu 0.5 (mỗi session dựng lại một tab, chưa có cửa sổ thì mở cửa sổ trước) thì để `open_tabs = true`.

**Thư mục chưa có mặt.** Project nằm trên ổ ngoài (`/Volumes/<tên>/...`) có thể vắng mặt lúc mới đăng nhập chỉ vì ổ chưa kịp mount. Lúc đó Kalmux dựng liền mấy session dựng được, chờ mấy volume đó tối đa 60 giây, rồi mới chịu thua. Session nào tới lúc đó vẫn chưa thấy thư mục thì mở ở `$HOME`, ghi rõ trong `ui.log`, thẻ có nhãn `folder missing` (đường dẫn bị thiếu cũng ghi ngay trên thẻ), và thư mục thiệt của nó được giữ trong session option `@kalmux_cwd`: chừng nào nó còn ngồi ở `$HOME` thì bản ghi vẫn ghi thư mục thiệt, nên lần dựng lại sau nó về đúng chỗ ngay khi thư mục có mặt lại. Thư mục có mặt rồi là nhãn tự mất, còn khi bạn dắt session đi chỗ khác thì `@kalmux_cwd` bị bỏ luôn: từ đó session thuộc về chỗ bạn để nó, kể cả `$HOME`. Trước 0.6, bản ghi đầu tiên sau cú lùi về `$HOME` đó ghi đè `$HOME` lên thư mục thiệt, mất luôn.

Chạy `tmux kill-server` cố ý thì nhìn y chang một cú crash, nên mấy session đó cũng trở về luôn. Muốn dẹp hẳn thì dẹp từng con: trên UI, `kalmux kill`, `tmux kill-session`, hoặc gõ `exit` ở pane cuối.

Chỉ tên, thư mục với màu trở về thôi: không window, không pane, không tiến trình đang chạy, không cuộc hội thoại. Session dựng lại là một cái shell nằm đúng thư mục, đúng thứ `claude --continue` cần.

```bash
kalmux restore --dry-run     # coi trước: tạo mới / đã có sẵn / thư mục chưa có mặt -> ~
kalmux restore               # làm bằng tay; tab theo [restore] open_tabs (muốn khác thì --tabs / --no-tabs)
kalmux open --restored       # rồi mở hết, chung một cửa sổ
```

`kalmux restore` đọc `sessions.json` chừng nào file đó còn thuộc về một server đã đi mất, còn khi server đang chạy đã lấy file đó rồi thì nó đọc `sessions.previous.json`. Nó không bao giờ ghi đè bản ghi, nên chạy bao nhiêu lần cũng được. Muốn giữ phần ghi nhận (và bản chép) mà tắt riêng phần tự động thì để `enabled = false` trong `[restore]`; danh sách mất kiểu đó vẫn có một dòng trong `ui.log`, nói luôn file nào đang giữ nó.

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

[restore]
# Hễ cái server tmux đang giữ session của bạn đi mất (reboot, iTerm2 crash hay thoát, tmux chết) là Kalmux
# dựng lại mọi session còn mở lúc đó: y tên, y thư mục, y màu. `kalmux restore` làm đúng việc đó bằng tay.
# Danh sách session thì lúc nào cũng được ghi; khóa này chỉ bật/tắt phần dựng lại tự động.
enabled = true
# Mở luôn mỗi session dựng lại một tab iTerm2. Để false thì session về mà không có tab, toolbelt đánh
# dấu "restored" kèm nút "Open all" (`kalmux open --restored` cũng làm được y vậy).
open_tabs = false
```

Ai quen xài `--dangerously-skip-permissions` thì bỏ vô hai mẫu lệnh này một lần, `new` với `resume` tự theo. Sửa xong có hiệu lực liền, khỏi khởi động lại.

**Thông báo.** Claude Code chọn kênh báo theo `TERM_PROGRAM`. tmux đặt biến đó thành `tmux`, nên trong tmux kênh mặc định `auto` không tìm ra cách nào và Claude im luôn, trong khi cũng Claude đó ở tab iTerm2 thường thì đăng thông báo macOS, có tiếng, ngay lúc nó cần mình. Cái im lặng này là thứ đầu tiên người ta để ý sau khi dọn hết agent vô tmux. Kalmux bù chỗ đó: đúng event `Notification` đó, hook đăng cùng một cảnh báo OSC 9 vô pane, iTerm2 hiện y như cũ. `iterm2 = "auto"` tự nhường ngay khi `~/.claude.json` có `preferredNotifChannel` của riêng bạn (`claude config set -g preferredNotifChannel iterm2` cũng chạy được trong tmux, vì Claude tự bọc chuỗi cho tmux), `"always"` thì đăng bất kể, `"never"` tắt hẳn, còn `bell = true` thêm chuông terminal, giống `iterm2_with_bell` của Claude. Entry hook riêng của iTerm2, chạy song song với của Kalmux, không đăng mấy cảnh báo này (cảnh báo đổi trạng thái của iTerm2 là cái công tắc một lần, bạn tự bật trong tool Session Status), nên hai bên không bao giờ báo trùng.

**Thư mục trạng thái:** vệt, bản ghi status và status line đã lưu đều nằm dưới `$KALMUX_STATE_DIR`, mặc định `~/.local/state/kalmux` (Kalmux không đọc `XDG_STATE_HOME`). Nếu bạn đổi biến này thì đặt ở chỗ mà Claude Code, hook và server UI đều thừa hưởng, vì file ghi theo giá trị này thì bên kia đọc bằng giá trị khác sẽ không thấy.

## Cái tap status line

`kalmux statusline` đứng trước status line bạn đang xài. Nó lưu một bản chuẩn hóa của từng lần refresh, chỗ mà vạch context đọc ra, rồi chuyền tối đa 1 MiB payload cho lệnh gốc của bạn và thoát với đúng mã thoát của lệnh đó, nên dấu nhắc của bạn vẫn hiện y như cũ. Tốn thêm khoảng 18 ms mỗi lần refresh.

Đối tượng `statusLine` gốc được lưu nguyên vẹn. `kalmux statusline uninstall` trả nó về chỗ cũ rồi đổi tên bản lưu chớ không xóa, nên lần cài sau nó ghi nhận đúng cái status line bạn đang xài lúc đó. Nếu bản lưu biến mất mà khóa vẫn đang đi qua Kalmux thì lệnh gỡ sẽ từ chối, không để bạn trơ trọi không còn status line nào, và `kalmux doctor` báo đỏ, chỉ thẳng tới file backup settings.

## An toàn

Server chỉ nghe trên loopback. Mọi lệnh gọi API đều mang một CSRF token riêng của tiến trình, đúc vô trang; `Host` với `Origin` bị ghim về loopback; trang chạy dưới CSP có nonce, không handler inline, không tài nguyên bên ngoài. Token này chống mấy trang web, chớ không chống được tiến trình khác cùng người dùng: thứ gì mở được socket loopback dưới danh nghĩa bạn thì cũng chạy thẳng `tmux kill-session` được.

Phần phát lại trạng thái chỉ ghi vô thiết bị ký tự dưới `/dev`, bản ghi tmux nào không giống id thiệt của tmux thì bị bỏ, và file backup settings ghi quyền 0600 vì trong đó có thể có API key.

## Trục trặc thì coi đâu

`kalmux doctor` kiểm file cấu hình, các entry hook riêng của Kalmux trong `settings.json` (đủ mọi event, trỏ đúng wrapper này, đúng chế độ hợp với iTerm2), `~/.config/iterm2/cc-status` đã về lại với iTerm2 chưa, iTerm2 có từ 3.7.5 trở lên không khi hook riêng của nó chạy song song với Kalmux, hook còn tới được Kalmux không (`last event 12s ago`, kèm tên mấy session chưa từng báo về), `jq`, khối trong tmux.conf, hai tùy chọn iTerm2, đăng ký toolbelt, script AutoLaunch với mấy đường dẫn nhúng trong đó, đường đi của status line với interpreter nướng vô đó, và server: có trả lời không, có đang chạy đúng phiên bản code này không, và nó thấy `tmux` ở đâu. Dòng đỏ nào cũng có kèm một dòng chỉ cách sửa.

`kalmux doctor --fix` chỉ chạy lại mấy bước đang hư mà chạy lại bao nhiêu lần cũng an toàn: hook của Claude Code, link CLI với alias, khối tmux.conf và lệnh status line (chỉ nướng lại lệnh đã chạy qua Kalmux sẵn: status line mà bạn giữ lại bằng `setup --no-statusline` thì `--fix` không bao giờ đụng tới). Nó in ra trước là sắp chạy gì. Nó không bao giờ ghi tùy chọn của iTerm2, script AutoLaunch hay toolbelt, cũng không tự mở server; mấy cái đó nó in sẵn lệnh để bạn tự chạy.

**Hook không còn tới.** Mỗi event, wrapper chạm vô file `hook.heartbeat` trong thư mục trạng thái, và file `hooks/<session id>` của đúng session gửi event đó. Không phải lần đổi trạng thái nào cũng có hook đi kèm (bấm Esc thì không có `Stop`, duyệt một permission prompt thì chẳng có gì cho tới khi tool chạy xong), nên báo động tính theo session chớ không theo từng lúc: khi một session Claude đang sống cứ đổi trạng thái hơn hai phút sau lúc entry được ghi (hay sau lúc nó khởi động) mà chưa gửi nổi một hook event nào, hoặc `settings.json` mất các entry của Kalmux, UI hiện một băng đỏ ghi tên mấy session đó (trạng thái, danh sách Gone và cảnh báo lúc đó không còn đúng) kèm nút **Repair** chỉ viết lại entry hook chớ không đụng gì khác, còn `kalmux doctor` báo đỏ. Thường thì Claude Code tự nạp lại hook đã sửa cho mấy session đang chạy; chỉ mở lại session nào mà băng đỏ vẫn còn ghi tên. Nếu entry đã có sẵn mà lỗi nằm ở một session chưa từng báo về, Repair cũng nói rõ vậy. Trong lúc hook không tới, Kalmux giữ hết vệt chớ không dọn mấy cái cũ hơn `keep_days`: đó là thứ duy nhất còn ghi lại cái gì để resume.

Sau khi sửa code của Kalmux thì chạy `kalmux ui restart` từ một shell bên trong iTerm2, để server giữ được mấy quyền TCC của iTerm2.

**Lệnh khởi động lại máy nằm chờ hoài.** Hễ có gì kêu iTerm2 thoát là nó hỏi "Quit iTerm2?", mà macOS thì đứng chờ cái hộp thoại đó trong lúc restart: máy nằm im tới chừng nào bạn trả lời, đi đâu một hồi quay lại nó vẫn còn nằm đó. Session thì không sao (iTerm2 chỉ detach mấy client tmux thôi), nhưng máy không khởi động lại được. Muốn bỏ chặn thì chạy `defaults write com.googlecode.iterm2 NeverBlockSystemShutdown -bool true` (Kalmux không bao giờ tự đụng vô tùy chọn này). Sau đó hộp thoại xác nhận chỉ còn hiện khi chính bạn bấm Cmd-Q.

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
