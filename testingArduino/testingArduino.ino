int redLED = 13;
int greenLED = 5;
int yellowLED = 9;
int buzzer = 3;
String input = "";

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
    if (input == "RED") {
      digitalWrite(yellowLED, LOW);
      digitalWrite(redLED, HIGH);
      digitalWrite(greenLED, LOW);
      tone(buzzer, 1000);
      noTone(buzzer);
      delay(10000);
    } else if (input == "GREEN") {
      digitalWrite(yellowLED, LOW);
      digitalWrite(redLED, LOW);
      digitalWrite(greenLED, HIGH);
      noTone(buzzer);
    }
    else {
      digitalWrite(yellowLED, HIGH);
      digitalWrite(redLED, LOW);
      digitalWrite(greenLED, LOW);
      noTone(buzzer);
    }
  }
}
