int redLED = 8;
int greenLED = 9;
int yellowLED = 10;
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
      delay(2000);
      digitalWrite(redLED, LOW);
      digitalWrite(yellowLED, HIGH);
    } else if (input == "GREEN") {
      digitalWrite(yellowLED, LOW);
      digitalWrite(redLED, LOW);
      digitalWrite(greenLED, HIGH);
      delay(2000);
      digitalWrite(greenLED, LOW);
      digitalWrite(yellowLED, HIGH);
    }
  }
}
