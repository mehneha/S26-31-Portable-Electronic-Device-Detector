int redLED = 13;
int greenLED = 5;
int yellowLED = 9;
int buzzer = 2;
String input = "";

void applyIdleLights(bool yellowEnabled) {
  digitalWrite(redLED, LOW);
  digitalWrite(greenLED, LOW);
  digitalWrite(yellowLED, yellowEnabled ? HIGH : LOW);
}

String getToken(const String& s, int index) {
  int start = 0;
  int tokenIndex = 0;
  int n = s.length();

  for (int i = 0; i <= n; i++) {
    if (i == n || s.charAt(i) == ',') {
      if (tokenIndex == index) {
        return s.substring(start, i);
      }
      tokenIndex++;
      start = i + 1;
    }
  }
  return "";
}

void applyLegacyState(const String& state) {
  if (state == "RED") {
    digitalWrite(yellowLED, LOW);
    digitalWrite(redLED, HIGH);
    digitalWrite(greenLED, LOW);
    tone(buzzer, 1000);
    delay(300);
    noTone(buzzer);
    applyIdleLights(true);
  } else if (state == "GREEN") {
    digitalWrite(yellowLED, LOW);
    digitalWrite(redLED, LOW);
    digitalWrite(greenLED, HIGH);
    noTone(buzzer);
  } else if (state == "YELLOW") {
    applyIdleLights(true);
    noTone(buzzer);
  } else {
    applyIdleLights(true);
    noTone(buzzer);
  }
}

void setup() {
  pinMode(redLED, OUTPUT);
  pinMode(greenLED, OUTPUT);
  pinMode(yellowLED, OUTPUT);
  Serial.begin(9600);
  digitalWrite(yellowLED, HIGH);
}

void loop() {
  if (Serial.available() > 0) {
    input = Serial.readStringUntil('\n');
    input.trim();

    if (input.startsWith("CFG,")) {
      String state = getToken(input, 1);
      bool redEnabled = getToken(input, 2).toInt() == 1;
      bool greenEnabled = getToken(input, 3).toInt() == 1;
      bool yellowEnabled = getToken(input, 4).toInt() == 1;
      bool speakerEnabled = getToken(input, 5).toInt() == 1;
      long redDuration = max(0L, getToken(input, 6).toInt());
      long greenDuration = max(0L, getToken(input, 7).toInt());
      long speakerDuration = max(0L, getToken(input, 8).toInt());
      speakerDuration = min(speakerDuration, redDuration);

      if (state == "RED") {
        digitalWrite(greenLED, LOW);
        digitalWrite(redLED, redEnabled ? HIGH : LOW);
        digitalWrite(yellowLED, redEnabled ? LOW : (yellowEnabled ? HIGH : LOW));

        if (speakerEnabled) {
          tone(buzzer, 1000);
        } else {
          noTone(buzzer);
        }

        if (speakerDuration > 0) {
          delay(speakerDuration);
        }
        noTone(buzzer);

        long redRemainder = redDuration - speakerDuration;
        if (redRemainder > 0) {
          delay(redRemainder);
        }
        applyIdleLights(yellowEnabled);

      } else if (state == "GREEN") {
        digitalWrite(redLED, LOW);
        digitalWrite(greenLED, greenEnabled ? HIGH : LOW);
        digitalWrite(yellowLED, greenEnabled ? LOW : (yellowEnabled ? HIGH : LOW));
        noTone(buzzer);
        if (greenEnabled && greenDuration > 0) {
          delay(greenDuration);
        }
        applyIdleLights(yellowEnabled);
      } else {
        applyIdleLights(yellowEnabled);
        noTone(buzzer);
      }
    } else {
      applyLegacyState(input);
    }
  }
}
