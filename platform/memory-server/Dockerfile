FROM eclipse-temurin:21-jre-alpine
WORKDIR /app

# Copy the pre-built JAR (build locally with Maven first)
# mvn package -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
#   -Dmaven.repo.local=./tmp-m2-repo -DskipTests
COPY target/*.jar app.jar

EXPOSE 8180

ENTRYPOINT ["java", "-jar", "app.jar"]
