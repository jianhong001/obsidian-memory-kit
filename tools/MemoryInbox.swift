// A named macOS app lets the user grant Documents-folder access to this watcher.
import AppKit
import Foundation

struct Settings: Decodable {
    let python: String
    let args: [String]
    let inbox: String
    let vault: String
    let status: String
}

func scan() throws -> Int32 {
    guard let config = Bundle.main.url(forResource: "settings", withExtension: "json") else {
        throw NSError(domain: "MemoryInbox", code: 1)
    }
    let s = try JSONDecoder().decode(Settings.self, from: Data(contentsOf: config))
    let manager = FileManager.default
    // This ordinary access uses the system consent dialog for the named app.
    // The tool never changes privacy settings or requests Full Disk Access.
    do {
        _ = try manager.contentsOfDirectory(atPath: s.inbox)
        _ = try manager.contentsOfDirectory(atPath: s.vault + "/02 模型对话")
    } catch {
        let status: [String: Any] = [
            "last_attempt": ISO8601DateFormatter().string(from: Date()),
            "blocked": "macOS 尚未允许模型对话收件箱访问指定的文稿目录。"
        ]
        try JSONSerialization.data(withJSONObject: status).write(to: URL(fileURLWithPath: s.status), options: .atomic)
        throw error
    }
    let child = Process()
    child.executableURL = URL(fileURLWithPath: s.python)
    child.arguments = s.args
    try child.run()
    child.waitUntilExit()
    return child.terminationStatus
}

do {
    exit(try scan())
} catch {
    FileHandle.standardError.write(Data("收件箱暂停。请检查‘隐私与安全性 → 文件与文件夹’中本应用的授权。\n".utf8))
    exit(1)
}
