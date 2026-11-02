param([string]$OutputDirectory = (Join-Path $PSScriptRoot '..\documents'))

$ErrorActionPreference = 'Stop'
# Новая схема строится из векторных блоков; исходный PNG не используется.
Add-Type -AssemblyName System.Drawing
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$width = 2400
$height = 1800
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
Text 80 94 'Одно ядро · обмен, возврат и локальные обращения · испытания τ-bench' 28 '#526478'

# Связи хранилища рисуются первыми: белая обводка потоков отделяет пересечения.
Arrow @(@(80,1180), @(35,1180), @(35,440), @(960,440), @(960,360), @(1000,360)) '#94A3B8'
Arrow @(@(770,600), @(770,485), @(1150,485), @(1150,420)) '#94A3B8' -Both
Arrow @(@(1000,1095), @(1090,1095), @(1090,520), @(1520,520), @(1520,420)) '#94A3B8' -Both
Text 90 450 'Результат доставки' 22 '#64748B'
Text 790 535 'Диалог и версии' 22 '#64748B'
Text 1160 530 'Условия, операция и очередь' 22 '#64748B'

Arrow @(@(430,290), @(500,290)) '#64748B'
Arrow @(@(890,290), @(1000,290)) '#64748B'
Arrow @(@(430,710), @(540,710)) '#2563EB' -Halo
Arrow @(@(1000,710), @(1840,710)) '#2563EB' -Both -Halo
Text 1170 660 'Сообщения · правила · схемы' 25 '#1D4ED8'
Arrow @(@(2065,600), @(2065,460)) '#2563EB' -Both
Text 2100 520 'generate()' 24 '#1D4ED8'
Arrow @(@(770,820), @(770,1060)) '#B45309'
Text 795 925 'Подтверждение' 24 '#92400E'
Arrow @(@(1000,790), @(1410,790), @(1410,1050)) '#15803D' -Both -Halo
Text 1110 875 'Чтения и предложения' 25 '#166534'
Arrow @(@(1000,1190), @(1160,1190)) '#B45309' -Both
Text 1010 1210 'check / execute' 20 '#92400E'
Arrow @(@(1650,1190), @(1840,1190)) '#15803D' -Both
Text 1690 1210 'Копия' 22 '#166534'
Arrow @(@(540,1180), @(430,1180)) '#B45309'
Text 445 1210 'Ответ' 22 '#92400E'

Box 80 200 350 180 'Исходные данные' 'retail/db.json + policy.md' @('Только чтение')
Box 500 200 390 180 'Инициализация' 'bootstrap.py' @('При пустом снимке')
Box 1000 160 650 260 'SQLite · Store' 'storage.py  |  var/support.sqlite3' @(
    'retail_state · правила · support_tickets',
    'Диалог · версии · согласия · подтверждения',
    'Операции · очередь · доставка · журнал'
) '#F8FAFC'

Rect 1840 160 450 300 '#EFF6FF' '#93C5FD'
Text 1864 180 'Выбор модели' 31 '#172B4D' -Bold
Text 1864 235 'demo_model.py · DemoModel' 24 '#253449'
Text 1864 273 'Учебные правила, без API' 25 '#526478'
Text 1864 317 'ИЛИ' 20 '#1D4ED8' -Bold
Text 1864 355 'llm.py · LiteLLMClient' 24 '#253449'
Text 1864 393 'Внешний API в режиме api' 25 '#526478'

Box 80 600 350 220 'Экран сотрудника' 'ui.py · Streamlit' @('История · предложения', 'Согласования · результат') '#EFF6FF' '#93C5FD'
Box 540 600 460 220 'Управление обращением' 'workflow.py · SupportService' @('Диалог · версии · пересогласование', 'Согласие клиента отдельно') '#EFF6FF' '#93C5FD'
Box 1840 600 450 220 'AgentCore' 'agent.py · ОДНО ЯДРО' @('Следующий шаг + состояние', 'Инструменты не исполняет') '#DBEAFE' '#2563EB'

Box 80 1060 350 240 'Тестовый канал' 'execution.py · TestChannel' @('Локальная запись в SQLite', 'Почта не отправляется') '#FFF7ED' '#FDBA74'
Box 540 1060 460 260 'Исполнитель' 'execution.py · Executor' @('Права · условия · согласования', 'tickets.py: регистрация заявки', 'Одна транзакция · без повтора') '#FFF7ED' '#FDBA74'
Box 1160 1050 490 270 'Адаптеры магазина' 'RetailAdapter · TicketRetailAdapter' @('Чтение: доступ ДО выдачи', 'Обмен / возврат: сумма + условия', 'Обращение: план без записи') '#F0FDF4' '#86EFAC'
Box 1840 1060 450 240 'RetailTools' 'tau2 · рабочая копия' @('Создаёт заявку обмена/возврата', 'Без платежа и письма') '#F0FDF4' '#86EFAC'
Text 80 1360 'Доставка после фиксации операции; её повтор не выполняет действие заново.' 25 '#526478'
Text 80 1400 'Обращение: номер T-{id} / open в SQLite; специалист и внешняя система не подключены.' 25 '#526478'

Rect 60 1470 2230 280 '#FAF8FF' '#D8CFF2'
Text 85 1490 'τ-bench · отдельный путь' 27 '#6D28D9' -Bold
Box 80 1550 500 160 'Штатная среда τ-bench' 'Окружение и оркестратор' @('Инструменты исполняет среда') '#FFFFFF' '#B6A3E2'
Box 730 1550 560 160 'Адаптер испытаний' 'adapters/tau_bench.py' @('Сообщения ↔ вызовы инструментов') '#FFFFFF' '#B6A3E2'
Arrow @(@(580,1630), @(730,1630)) '#7C3AED' -Both
Arrow @(@(1290,1630), @(2350,1630), @(2350,710), @(2290,710)) '#7C3AED' -Both -Dashed
Text 1450 1582 'К ТОМУ ЖЕ AgentCore' 27 '#6D28D9' -Bold
Text 1450 1660 'Согласования сотрудника сюда не добавляются' 23 '#6D28D9'

$bitmap.Save((Join-Path $OutputDirectory 'architecture.png'), [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bitmap.Dispose()
Write-Output "Создан architecture.png в $OutputDirectory"
