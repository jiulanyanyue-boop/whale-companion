package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Test

class GalleryResolutionTest {
    @Test fun preservesReadablePhoneScreenshotResolution() {
        assertEquals(1, galleryDecodeSample(1080, 2388, 2560))
        assertEquals(1, galleryDecodeSample(512, 512, 2560))
    }

    @Test fun largePhotosDecodeAboveTheFinalEdgeLimit() {
        assertEquals(2, galleryDecodeSample(4000, 8000, 2560))
        assertEquals(1, galleryDecodeSample(1500, 3300, 2560))
    }
}
