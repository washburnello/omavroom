// Queue sidebar: waiters with position, type, agent, project, wait time,
// and who is next.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: panel
    objectName: "queuePanel"
    property int waiterCount: queueList.count
    color: "#0d1117"
    radius: 8
    border.width: 1
    border.color: "#21262d"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 6

        RowLayout {
            Layout.fillWidth: true
            Label { text: "QUEUE"; color: "#e6edf3"; font.bold: true }
            Item { Layout.fillWidth: true }
            Label { text: backend.queueCount + " waiting"; color: "#8b949e" }
        }

        Label {
            Layout.fillWidth: true
            visible: backend.queueCount === 0
            text: "no waiters"
            color: "#4b535d"
        }

        ListView {
            id: queueList
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: backend.queue
            spacing: 4

            delegate: Rectangle {
                required property var modelData
                width: queueList.width
                height: 46
                radius: 5
                color: modelData.next_up ? "#10251a" : "#141922"
                border.width: 1
                border.color: modelData.next_up ? "#2ea043" : "#21262d"

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 7
                    spacing: 8

                    Rectangle {
                        visible: modelData.next_up
                        color: "#2ea043"
                        radius: 3
                        implicitWidth: nextLabel.implicitWidth + 8
                        implicitHeight: nextLabel.implicitHeight + 2
                        Label {
                            id: nextLabel
                            anchors.centerIn: parent
                            text: "NEXT"
                            color: "#03170a"
                            font.bold: true
                            font.pixelSize: 10
                        }
                    }
                    Label { text: "#" + modelData.position; color: "#8b949e" }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        Label {
                            Layout.fillWidth: true
                            text: modelData.agent
                                + (modelData.project !== "-" ? "  [" + modelData.project + "]" : "")
                            color: "#c9d1d9"
                            elide: Text.ElideRight
                        }
                        Label {
                            text: modelData.seat_type + " · waited " + modelData.waited
                                + " · ahead " + modelData.queue_ahead
                            color: "#6e7681"
                            font.pixelSize: 11
                        }
                    }
                }
            }
        }
    }
}
