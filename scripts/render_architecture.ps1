param([string]$OutputDirectory = (Join-Path $PSScriptRoot '..\documents'))

$ErrorActionPreference = 'Stop'
# Новая схема строится из векторных блоков; исходный PNG не используется.
Add-Type -AssemblyName System.Drawing
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$width = 2400
$height = 2370
$bitmap = [System.Drawing.Bitmap]::new($width, $height)
$bitmap.SetResolution(144, 144)
$g = [System.Drawing.Graphics]::FromImage($bitmap)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$g.Clear([System.Drawing.Color]::White)

function Color([string]$Value) { [System.Drawing.ColorTranslator]::FromHtml($Value) }

function Text($x, $y, [string]$value, $size = 25, $color = '#253449', [switch]$Bold) {
    $style = if ($Bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
    $font = [System.Drawing.Font]::new('Segoe UI', $size, $style, [System.Drawing.GraphicsUnit]::Pixel)
    $brush = [System.Drawing.SolidBrush]::new((Color $color))
    $g.DrawString($value, $font, $brush, [System.Drawing.PointF]::new($x, $y))
    $font.Dispose()
    $brush.Dispose()
}

function Rect($x, $y, $w, $h, $fill = '#FFFFFF', $stroke = '#CBD5E1', $radius = 14) {
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $d = 2 * $radius
    $path.AddArc($x, $y, $d, $d, 180, 90)
    $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    $brush = [System.Drawing.SolidBrush]::new((Color $fill))
    $pen = [System.Drawing.Pen]::new((Color $stroke), 2)
    $g.FillPath($brush, $path)
    $g.DrawPath($pen, $path)
    $brush.Dispose(); $pen.Dispose(); $path.Dispose()
}

function ArrowHead($from, $to, $color) {
    $dx = $to[0] - $from[0]; $dy = $to[1] - $from[1]
    $length = [Math]::Sqrt($dx * $dx + $dy * $dy)
    $ux = $dx / $length; $uy = $dy / $length
    $a = @(($to[0] - 15 * $ux + 7 * $uy), ($to[1] - 15 * $uy - 7 * $ux))
    $b = @(($to[0] - 15 * $ux - 7 * $uy), ($to[1] - 15 * $uy + 7 * $ux))
    [System.Drawing.PointF[]]$triangle = @(
        [System.Drawing.PointF]::new($to[0], $to[1]),
        [System.Drawing.PointF]::new($a[0], $a[1]),
        [System.Drawing.PointF]::new($b[0], $b[1])
    )
    $brush = [System.Drawing.SolidBrush]::new((Color $color))
    $g.FillPolygon($brush, $triangle)
    $brush.Dispose()
}

function Arrow($points, $color = '#64748B', [switch]$Both, [switch]$Dashed, [switch]$Halo) {
    if ($Halo) {
        $white = [System.Drawing.Pen]::new([System.Drawing.Color]::White, 11)
        for ($i = 1; $i -lt $points.Count; $i++) {
            $g.DrawLine($white, $points[$i-1][0], $points[$i-1][1], $points[$i][0], $points[$i][1])
        }
        $white.Dispose()
    }
    $pen = [System.Drawing.Pen]::new((Color $color), 3)
    if ($Dashed) { $pen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash }
    for ($i = 1; $i -lt $points.Count; $i++) {
        $g.DrawLine($pen, $points[$i-1][0], $points[$i-1][1], $points[$i][0], $points[$i][1])
    }
    ArrowHead $points[-2] $points[-1] $color
    if ($Both) { ArrowHead $points[1] $points[0] $color }
    $pen.Dispose()
}

function Box($x, $y, $w, $h, $title, $module, $lines, $fill = '#FFFFFF', $stroke = '#94A3B8') {
    Rect $x $y $w $h $fill $stroke
    Text ($x + 24) ($y + 20) $title 31 '#172B4D' -Bold
    Text ($x + 24) ($y + 66) $module 23 '#526478'
    $lineY = $y + 112
    foreach ($line in $lines) {
        Text ($x + 24) $lineY $line 25
        $lineY += 36
    }
}

Text 80 35 'ИИ-агент поддержки · реализованная архитектура' 44 '#172B4D' -Bold
Text 80 94 'Одно ядро · retail · local | zammad · сообщения только в тестовый канал' 28 '#526478'

# Серые линии — короткие обращения к SQLite. Потоки поверх них имеют белую обводку.
Arrow @(@(840,620), @(840,505), @(1400,505), @(1400,440)) '#94A3B8' -Both
Arrow @(@(1110,1160), @(1145,1160), @(1145,535), @(1810,535), @(1810,440)) '#94A3B8' -Both
Arrow @(@(80,1240), @(35,1240), @(35,475), @(1300,475), @(1300,440)) '#94A3B8'
Text 865 545 'Диалог и версии' 22 '#64748B'
Text 1490 490 'Короткие транзакции' 22 '#64748B'
Text 85 490 'Сохранение доставки' 22 '#64748B'

Arrow @(@(480,285), @(560,285))
Arrow @(@(1120,285), @(1210,285))
Arrow @(@(490,735), @(570,735)) '#2563EB' -Both -Halo
Arrow @(@(1110,735), @(1200,735)) '#2563EB' -Both -Halo
Arrow @(@(1700,735), @(1810,735)) '#2563EB' -Both
Text 1225 575 'Сообщения · правила · схемы' 24 '#1D4ED8'
Text 1875 575 'generate() · demo / api' 24 '#1D4ED8'
Arrow @(@(840,850), @(840,1090)) '#B45309'
Text 585 900 'Подтверждение' 24 '#92400E'
Text 585 934 'сотрудника' 24 '#92400E'
Arrow @(@(1050,850), @(1050,970), @(1450,970), @(1450,1090)) '#15803D' -Both -Halo
Text 1165 1002 'Чтения и предложения' 24 '#166534'
Arrow @(@(1110,1230), @(1200,1230)) '#B45309' -Both -Halo
Arrow @(@(1700,1230), @(1810,1230)) '#15803D' -Both
Text 1720 1260 'Копия' 21 '#166534'
Arrow @(@(570,1230), @(490,1230)) '#B45309'

Box 80 180 400 210 'Исходные данные' 'retail/db.json + policy.md' @('Только чтение', 'Начальный снимок')
Box 560 180 560 240 'Настройки и запуск' 'bootstrap.py' @(
    'Модель и место регистрации независимы',
    'URL · группа · соответствие клиентов',
    'Токен не входит в предложение'
)
Box 1210 180 1110 260 'SQLite · Store' 'storage.py · var/support.sqlite3' @(
    'retail_state · диалог · версии · согласия · подтверждения',
    'support_tickets · external_ticket_operations',
    'operations · outbox · доставка · журнал'
) '#F8FAFC'

Box 80 620 410 230 'Экран сотрудника' 'ui.py · Streamlit' @('История · точное предложение', 'Согласование · результат') '#EFF6FF' '#93C5FD'
Box 570 620 540 230 'Управление обращением' 'workflow.py · SupportService' @('Диалог · версии · пересогласование', 'Согласие клиента отдельно') '#EFF6FF' '#93C5FD'
Box 1200 620 500 230 'AgentCore' 'agent.py · ОДНО ЯДРО' @('Следующий шаг + состояние', 'Инструменты не исполняет') '#DBEAFE' '#2563EB'
Box 1810 620 510 230 'Выбор модели' 'DemoModel ИЛИ LiteLLMClient' @('demo: заданные учебные правила', 'api: внешний API модели') '#EFF6FF' '#93C5FD'
Text 80 905 'В согласовании: local | zammad,' 24 '#1D4ED8'
Text 80 940 'URL, группа, тестовый клиент,' 24 '#1D4ED8'
Text 80 975 'тема, текст заявки и ответ.' 24 '#1D4ED8'

Box 80 1090 410 260 'Тестовый канал' 'execution.py · TestChannel' @('Прежний согласованный текст', 'Постоянный ключ доставки', 'Без почты и ответа специалиста') '#FFF7ED' '#FDBA74'
Box 570 1090 540 260 'Исполнитель' 'execution.py · Executor' @('Права · условия · согласования', 'local: tickets.py · одна транзакция', 'zammad: external_tickets.py') '#FFF7ED' '#FDBA74'
Box 1200 1090 500 260 'Адаптеры магазина' 'RetailAdapter · TicketRetailAdapter' @('Доступ ДО выдачи данных', 'Суммы и условия проверяет код', 'Предложение без записи') '#F0FDF4' '#86EFAC'
Box 1810 1090 510 260 'RetailTools' 'tau2 · рабочая копия' @('Заявки обмена и возврата', 'Без платежей и писем') '#F0FDF4' '#86EFAC'
Text 80 1385 'local: support_tickets → operations + outbox' 24 '#526478'
Arrow @(@(840,1350), @(840,1470)) '#B45309'
Text 865 1385 'target = zammad' 24 '#92400E'
Text 1210 1385 'Новые параметры требуют пересогласования' 24 '#526478'

Rect 60 1470 2260 500 '#FFFBF4' '#FDBA74'
Text 85 1490 'Zammad · external_tickets.py + adapters/zammad.py' 30 '#92400E' -Bold
Text 85 1540 'Сеть вне SQLite. Недоступность Zammad не переключает регистрацию на local.' 25 '#526478'
Box 90 1600 630 240 '1. До POST' 'SQLite · submitting' @(
    'Постоянный operation_id · согласование',
    'Параметры заявки · точный ответ',
    'Фиксация до сетевого вызова'
) '#FFFFFF' '#FDBA74'
Box 840 1600 630 240 '2. Один POST → проверка GET' 'ZammadBackend · внешний API' @(
    'Прочитать созданную заявку и статью',
    'Сверить номер, клиента, группу, тему,',
    'содержание и допустимый статус'
) '#FFFFFF' '#FDBA74'
Box 1590 1600 630 240 '3. Проверенный результат' 'SQLite · verified' @(
    'operations + outbox в одной транзакции',
    'На экране: номер · статус · ссылка',
    'Доставка прежнего текста в TestChannel'
) '#FFFFFF' '#FDBA74'
Arrow @(@(720,1720), @(840,1720)) '#B45309'
Arrow @(@(1470,1720), @(1590,1720)) '#B45309'
Text 90 1870 'submitting / unknown → «Проверить результат в Zammad» → только GET; повторный POST заблокирован.' 25 '#92400E' -Bold
Text 90 1915 'Не найдено совпадение — остаётся unknown. Найдено и проверено — verified и прежний ответ; нового согласования нет.' 24 '#526478'
Text 80 1990 'Создание заявки не подтверждает решение проблемы. Проверки без API не заменяют облачную приёмку.' 25 '#526478'

Rect 60 2050 2260 280 '#FAF8FF' '#D8CFF2'
Text 85 2070 'τ-bench · отдельный путь' 27 '#6D28D9' -Bold
Box 80 2130 500 160 'Штатная среда τ-bench' 'Окружение и оркестратор' @('Инструменты исполняет среда') '#FFFFFF' '#B6A3E2'
Box 730 2130 560 160 'Адаптер испытаний' 'adapters/tau_bench.py' @('Сообщения ↔ вызовы инструментов') '#FFFFFF' '#B6A3E2'
Arrow @(@(580,2210), @(730,2210)) '#7C3AED' -Both
Arrow @(@(1290,2210), @(2350,2210), @(2350,565), @(1580,565), @(1580,620)) '#7C3AED' -Both -Dashed
Text 1450 2162 'К ТОМУ ЖЕ AgentCore' 27 '#6D28D9' -Bold
Text 1450 2240 'Согласования сотрудника сюда не добавляются' 23 '#6D28D9'

$bitmap.Save((Join-Path $OutputDirectory 'architecture.png'), [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bitmap.Dispose()
Write-Output "Создан architecture.png в $OutputDirectory"
