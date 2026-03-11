"""
NEO Trade Terminal - Sound Alerts Manager

Sound alerts for order events.
Uses system sounds or custom WAV files.
"""

import os
import threading
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class SoundAlertManager:
    """Manages sound alerts for trading events."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        ui_prefs = config.get('ui_preferences', {})
        self.enabled = ui_prefs.get('sound_on_fill', True)
        self.sounds_dir = "assets/sounds"

        # Sound mapping
        self.sound_files: Dict[str, str] = {
            'order_placed': 'click.wav',
            'order_filled': 'success.wav',
            'order_rejected': 'error.wav',
            'sl_hit': 'alert.wav',
            'target_hit': 'cash.wav',
            'position_exit': 'ding.wav',
            'error': 'error.wav',
            'warning': 'alert.wav',
            'price_alert': 'bell.wav',  # Price alert sound
        }

        # Frequency mapping for system beeps
        self.freq_map: Dict[str, int] = {
            'order_placed': 800,
            'order_filled': 1200,
            'order_rejected': 400,
            'sl_hit': 300,
            'target_hit': 1500,
            'position_exit': 1000,
            'error': 350,
            'warning': 600,
            'price_alert': 1000,  # Bell-like tone
        }

        # Duration mapping (ms)
        self.duration_map: Dict[str, int] = {
            'order_placed': 150,
            'order_filled': 300,
            'order_rejected': 400,
            'sl_hit': 500,
            'target_hit': 400,
            'position_exit': 250,
            'error': 400,
            'warning': 300,
            'price_alert': 200,  # Short bell for each ring
        }

        # S38: Limit concurrent sound threads to prevent accumulation under rapid events
        self._sound_semaphore = threading.Semaphore(3)

        # Try to import sound library
        self._sound_lib: Optional[str] = None
        self._init_sound_library()

    def _init_sound_library(self):
        """Initialize sound library."""
        # Try winsound first (Windows)
        try:
            import winsound
            self._sound_lib = 'winsound'
            logger.info("Sound: Using winsound")
            return
        except ImportError:
            pass

        # Try playsound
        try:
            from playsound import playsound
            self._sound_lib = 'playsound'
            logger.info("Sound: Using playsound")
            return
        except ImportError:
            pass

        # Try simpleaudio
        try:
            import simpleaudio
            self._sound_lib = 'simpleaudio'
            logger.info("Sound: Using simpleaudio")
            return
        except ImportError:
            pass

        logger.warning("Sound: No sound library available")

    def play(self, event_type: str):
        """
        Play sound for event type (non-blocking).

        Args:
            event_type: Type of event (order_placed, order_filled, etc.)
        """
        if not self.enabled or not self._sound_lib:
            return

        # S38: Use semaphore to limit concurrent sound threads
        if not self._sound_semaphore.acquire(blocking=False):
            return  # Skip if too many sounds playing

        # Play in background thread to not block
        threading.Thread(
            target=self._play_sound_with_release,
            args=(event_type,),
            daemon=True
        ).start()

    def _play_sound_with_release(self, event_type: str):
        """Play sound and release semaphore when done."""
        try:
            self._play_sound(event_type)
        finally:
            self._sound_semaphore.release()

    def _play_sound(self, event_type: str):
        """Internal sound player."""
        sound_file = self.sound_files.get(event_type)
        if not sound_file:
            # Use system beep as fallback
            self._beep(event_type)
            return

        filepath = os.path.join(self.sounds_dir, sound_file)

        if os.path.exists(filepath):
            try:
                if self._sound_lib == 'winsound':
                    import winsound
                    winsound.PlaySound(
                        filepath,
                        winsound.SND_FILENAME | winsound.SND_ASYNC
                    )

                elif self._sound_lib == 'playsound':
                    from playsound import playsound
                    playsound(filepath, block=False)

                elif self._sound_lib == 'simpleaudio':
                    import simpleaudio as sa
                    wave_obj = sa.WaveObject.from_wave_file(filepath)
                    wave_obj.play()

            except Exception as e:
                logger.warning(f"Sound playback error: {e}")
                self._beep(event_type)
        else:
            self._beep(event_type)

    def _beep(self, event_type: str):
        """System beep fallback."""
        try:
            if self._sound_lib == 'winsound':
                import winsound
                freq = self.freq_map.get(event_type, 600)
                duration = self.duration_map.get(event_type, 200)
                winsound.Beep(freq, duration)
            else:
                # Terminal bell
                print('\a', end='', flush=True)
        except Exception:
            pass

    def toggle(self, enabled: bool):
        """
        Enable/disable sounds.

        Args:
            enabled: True to enable, False to disable
        """
        self.enabled = enabled
        logger.info(f"Sound alerts {'enabled' if enabled else 'disabled'}")

    def is_enabled(self) -> bool:
        """Check if sounds are enabled."""
        return self.enabled

    def test_all(self):
        """Test all sound alerts."""
        import time

        print("Testing all sound alerts...")
        for event in self.sound_files.keys():
            print(f"  Testing: {event}")
            self._play_sound(event)
            time.sleep(1)
        print("Sound test complete.")

    def test(self, event_type: str):
        """
        Test a specific sound alert.

        Args:
            event_type: Event type to test
        """
        print(f"Testing sound: {event_type}")
        self._play_sound(event_type)

    def play_custom_beep(self, frequency: int, duration: int):
        """
        Play a custom beep (Windows only).

        Args:
            frequency: Beep frequency in Hz
            duration: Duration in milliseconds
        """
        if not self.enabled:
            return

        if self._sound_lib == 'winsound':
            try:
                import winsound
                winsound.Beep(frequency, duration)
            except Exception as e:
                logger.warning(f"Custom beep failed: {e}")

    def play_price_alert(self):
        """
        Play price alert sound - 3 distinct bell tones.

        Plays 3 sequential beeps with increasing frequency
        to create an attention-grabbing alert sound.
        Non-blocking (runs in background thread).
        """
        if not self.enabled:
            return

        # Play in background thread to not block UI
        threading.Thread(
            target=self._play_three_bells,
            daemon=True
        ).start()

    def _play_three_bells(self):
        """Internal: Play 3 ascending bell tones."""
        import time

        # Bell frequencies: C5, E5, G5 (ascending major chord)
        bell_freqs = [523, 659, 784]  # Hz
        bell_duration = 180  # ms

        for freq in bell_freqs:
            try:
                if self._sound_lib == 'winsound':
                    import winsound
                    winsound.Beep(freq, bell_duration)
                else:
                    # Fallback to terminal bell
                    print('\a', end='', flush=True)
                time.sleep(0.1)  # 100ms gap between bells
            except Exception as e:
                logger.warning(f"Bell sound failed: {e}")
                break
