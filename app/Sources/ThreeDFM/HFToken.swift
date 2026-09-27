import Foundation
import Security

// MARK: - Hugging Face token in Keychain (never in files or logs)

enum HFTokenStore {
    private static let service = "local.3dfm.hf-token"
    private static let account = "huggingface"

    /// Returns the stored token, a bundled/test override, or "".
    static func load() -> String {
        if let env = ProcessInfo.processInfo.environment["HF_TOKEN"],
           !env.isEmpty {
            return env
        }
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let tok = String(data: data, encoding: .utf8),
              !tok.isEmpty
        else { return "" }
        return tok
    }

    static func save(_ token: String) {
        delete()
        guard !token.isEmpty else { return }
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: Data(token.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        SecItemAdd(q as CFDictionary, nil)
    }

    static func delete() {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(q as CFDictionary)
    }
}
