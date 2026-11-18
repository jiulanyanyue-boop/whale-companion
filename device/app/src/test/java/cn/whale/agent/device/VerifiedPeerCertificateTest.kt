package cn.whale.agent.device

import java.io.File
import java.nio.file.Files
import java.security.KeyStore
import java.security.cert.X509Certificate
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLServerSocket
import okhttp3.Request
import org.junit.Assert.*
import org.junit.Test

class VerifiedPeerCertificateTest {
    private fun runTlsTest(correctPin: Boolean) {
        val store = KeyStore.getInstance("PKCS12")
        val dir = Files.createTempDirectory("whale-tls-test")
        val path = dir.resolve("unit-test.p12")
        try {
            val executable = if (System.getProperty("os.name").startsWith("Windows")) "keytool.exe" else "keytool"
            val tool = File(System.getProperty("java.home"), "bin/$executable")
            val process = ProcessBuilder(tool.path, "-genkeypair", "-alias", "test", "-keyalg", "RSA",
                "-dname", "CN=localhost", "-validity", "2", "-storetype", "PKCS12", "-keystore", path.toString(),
                "-storepass", "test-only", "-noprompt").redirectErrorStream(true).start()
            assertTrue("keytool timed out", process.waitFor(30, TimeUnit.SECONDS))
            assertEquals(process.inputStream.bufferedReader().use { it.readText() }, 0, process.exitValue())
            Files.newInputStream(path).use { store.load(it, "test-only".toCharArray()) }
        } finally {
            Files.deleteIfExists(path)
            Files.deleteIfExists(dir)
        }
        val cert = store.getCertificate("test") as X509Certificate
        val managers = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm())
        managers.init(store, "test-only".toCharArray())
        val context = SSLContext.getInstance("TLS")
        context.init(managers.keyManagers, null, null)
        val server = context.serverSocketFactory.createServerSocket(0, 1,
            java.net.InetAddress.getLoopbackAddress()) as SSLServerSocket
        server.soTimeout = 5000
        val worker = Executors.newSingleThreadExecutor()
        val response = worker.submit {
            runCatching {
                server.accept().use { socket ->
                    socket.soTimeout = 5000
                    val reader = socket.getInputStream().bufferedReader()
                    while (!reader.readLine().isNullOrEmpty()) { }
                    socket.getOutputStream().write(
                        "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK".toByteArray(),
                    )
                    socket.getOutputStream().flush()
                }
            }
        }
        val trusted = AtomicReference<X509Certificate>()
        val pin = if (correctPin) CertificatePinning.sha256Hex(cert) else "0".repeat(64)
        val client = CertificatePinning.clientFor(pin) { trusted.set(it) }
        try {
            val request = Request.Builder().url("https://127.0.0.1:${server.localPort}/").build()
            if (correctPin) {
                client.newCall(request).execute().use { received ->
                    assertEquals("OK", received.body!!.string())
                    assertNotNull("certificate must be captured during pin verification", trusted.get())
                    assertEquals(pin, CertificatePinning.sha256Hex(trusted.get()))
                    // With a private trust anchor OkHttp may expose an empty cleaned chain.
                    // Authentication must still have the verified certificate above.
                }
            } else {
                try {
                    client.newCall(request).execute().close()
                    fail("a mismatched certificate must not be accepted")
                } catch (expected: java.io.IOException) {
                    assertNull("untrusted certificate must never reach callback", trusted.get())
                }
            }
            response.get(6, TimeUnit.SECONDS)
        } finally {
            server.close()
            worker.shutdownNow()
            client.dispatcher.executorService.shutdownNow()
            client.connectionPool.evictAll()
        }
    }

    @Test fun verifiedCertificateIsAvailableWithoutOkHttpCleanedChain() = runTlsTest(true)
    @Test fun mismatchedCertificateIsNeverDeliveredToConnection() = runTlsTest(false)

    @Test fun connectionErrorsDoNotExposeExceptionDetails() {
        val secret = "token-do-not-display"
        assertFalse(ConnectionSettings.connectionFailureMessage(java.net.ConnectException(secret)).contains(secret))
        assertTrue(ConnectionSettings.connectionFailureMessage(javax.net.ssl.SSLHandshakeException(secret)).contains("TLS"))
    }
}
