// 画 Echobook的 1024×1024 图标：暖棕底、打开的书、右上角声波。用法：swift make_icon.swift out.png
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments[1]
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// 背景：macOS 图标网格的圆角方块 + 纵向渐变
let bg = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
NSGradient(starting: NSColor(red: 0.80, green: 0.42, blue: 0.24, alpha: 1),
           ending: NSColor(red: 0.55, green: 0.24, blue: 0.13, alpha: 1))!.draw(in: bg, angle: -90)

// 打开的书：两页，书脊在中间
let paper = NSColor(red: 0.98, green: 0.95, blue: 0.89, alpha: 1)
func page(_ left: Bool) -> NSBezierPath {
    let p = NSBezierPath()
    let spine: CGFloat = 512, outer: CGFloat = left ? 250 : 774
    p.move(to: NSPoint(x: spine, y: 300))
    p.curve(to: NSPoint(x: outer, y: 330), controlPoint1: NSPoint(x: spine + (outer - spine) * 0.35, y: 255),
            controlPoint2: NSPoint(x: spine + (outer - spine) * 0.75, y: 300))
    p.line(to: NSPoint(x: outer, y: 640))
    p.curve(to: NSPoint(x: spine, y: 610), controlPoint1: NSPoint(x: spine + (outer - spine) * 0.75, y: 610),
            controlPoint2: NSPoint(x: spine + (outer - spine) * 0.35, y: 565))
    p.close()
    return p
}
paper.setFill(); page(true).fill(); page(false).fill()

// 左页的文字行（第三行是高亮中的那个词）
let ink = NSColor(red: 0.55, green: 0.24, blue: 0.13, alpha: 0.45)
for (i, y) in [540, 490, 440, 390].enumerated() {
    let line = NSBezierPath(roundedRect: NSRect(x: 300, y: CGFloat(y) - CGFloat(i) * 4, width: 170, height: 16),
                            xRadius: 8, yRadius: 8)
    ink.setFill(); line.fill()
}
NSColor(red: 1.0, green: 0.80, blue: 0.35, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 300, y: 425, width: 90, height: 30), xRadius: 10, yRadius: 10).fill()

// 右页上方的声波
NSColor(red: 0.55, green: 0.24, blue: 0.13, alpha: 0.85).setStroke()
for (i, r) in [60, 110, 160].enumerated() {
    let arc = NSBezierPath()
    arc.appendArc(withCenter: NSPoint(x: 570, y: 450), radius: CGFloat(r), startAngle: -38, endAngle: 38)
    arc.lineWidth = 26 - CGFloat(i) * 3
    arc.lineCapStyle = .round
    arc.stroke()
}

NSGraphicsContext.current = nil
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
