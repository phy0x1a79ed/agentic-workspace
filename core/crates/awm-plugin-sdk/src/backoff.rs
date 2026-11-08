//! The wait between reconnect attempts: it grows by a factor up to a cap and starts over on success.

use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Backoff {
    initial: Duration,
    max: Duration,
    factor: f64,
    next: Duration,
}

impl Backoff {
    pub fn new(initial: Duration, max: Duration, factor: f64) -> Self {
        Self { initial, max, factor, next: initial }
    }

    /// Doubling from `initial` seconds to `max` seconds.
    pub fn doubling(initial_s: f64, max_s: f64) -> Self {
        Self::new(Duration::from_secs_f64(initial_s), Duration::from_secs_f64(max_s), 2.0)
    }

    /// The wait to sleep now; the next call returns a longer one.
    pub fn wait(&mut self) -> Duration {
        let wait = self.next;
        self.next = Duration::from_secs_f64((wait.as_secs_f64() * self.factor).min(self.max.as_secs_f64()));
        wait
    }

    pub fn reset(&mut self) {
        self.next = self.initial;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn it_grows_to_the_cap_and_resets() {
        let mut b = Backoff::doubling(1.0, 5.0);
        let waits: Vec<u64> = (0..5).map(|_| b.wait().as_secs()).collect();
        assert_eq!(waits, [1, 2, 4, 5, 5]);
        b.reset();
        assert_eq!(b.wait(), Duration::from_secs(1));
    }
}
