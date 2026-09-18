// One permanent monitor slot. Desktop seats render a live downscaled
// screenshot; terminal seats render their live exec text. A torn-down seat
// leaves the tile in place showing "off / no signal".
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: tile
    objectName: "slotTile-" + (slotData.key || "")
    property var slotData: ({})
    signal peekRequested()

    readonly property bool occupied: slotData.occupied === true
    readonly property bool desktop: slotData.seat_type === "desktop"
    readonly property string thumb: slotData.thumbnail_source || ""
    readonly property string terminal: slotData.terminal_text || ""
    readonly property string offText: slotData.off_text || "off / no signal"
    // Terminals are headless: they have no viewer endpoint, so they must not
    // offer click-to-peek. The backend also rejects it defensively.
    readonly property bool peekable: slotData.peekable === true

    radius: 8
    color: occupied ? "#151a21" : "#0f1216"
    border.width: 1
    border.color: slotData.needsAttention ? "#f85149" : "#262d36"
    clip: true

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        // The "screen": thumbnail, terminal text, or off/no-signal.
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 5
            color: "#04060a"
            border.width: 1
            border.color: "#1c222b"
            clip: true

            Image {
                id: thumbImage
                anchors.fill: parent
                anchors.margins: 1
                visible: tile.desktop && tile.occupied && tile.thumb !== ""
                source: tile.thumb
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                cache: false
                smooth: true
                mipmap: true
            }

            Text {
                anchors.fill: parent
                anchors.margins: 8
                visible: !thumbImage.visible
                text: {
                    if (!tile.occupied)
                        return tile.offText;
                    if (tile.desktop)
                        return (tile.slotData.state || "") + "\n(no signal yet)";
                    return tile.terminal !== "" ? tile.terminal : "idle";
                }
                color: tile.occupied ? "#8b949e" : "#4b535d"
                font.family: tile.desktop ? "sans-serif" : "monospace"
                font.pixelSize: tile.desktop ? 13 : 11
                wrapMode: Text.WrapAnywhere
                elide: Text.ElideRight
                verticalAlignment: Text.AlignTop
            }
        }

        // Header labels: slot name, type, state.
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Label {
                text: slotData.name || ""
                color: "#e6edf3"
                font.bold: true
                elide: Text.ElideRight
            }
            Rectangle {
                color: "#1c222b"
                radius: 3
                implicitWidth: typeLabel.implicitWidth + 10
                implicitHeight: typeLabel.implicitHeight + 2
                Label {
                    id: typeLabel
                    anchors.centerIn: parent
                    text: slotData.seat_type || ""
                    color: "#79c0ff"
                    font.pixelSize: 11
                }
            }
            Item { Layout.fillWidth: true }
            Label {
                text: slotData.state || ""
                color: slotData.needs_attention ? "#f85149" : "#9aa4b2"
            }
        }

        // Identity labels: agent + project.
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Label {
                Layout.fillWidth: true
                text: "agent " + (slotData.agent || "-")
                color: "#c9d1d9"
                elide: Text.ElideRight
            }
            Label {
                Layout.fillWidth: true
                text: "project " + (slotData.project || "-")
                color: "#8b949e"
                elide: Text.ElideRight
            }
        }

        // Time labels: elapsed, lease, heartbeat.
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Label { text: "elapsed " + (slotData.elapsed || "-"); color: "#8b949e"; font.pixelSize: 12 }
            Label { text: "lease " + (slotData.lease || "-"); color: "#8b949e"; font.pixelSize: 12 }
            Label { text: "hb " + (slotData.heartbeat || "-"); color: "#8b949e"; font.pixelSize: 12 }
            Item { Layout.fillWidth: true }
        }
    }

    HoverHandler { id: hover }
    ToolTip.visible: hover.hovered && tile.occupied && tile.peekable
    ToolTip.text: "Click to peek (shows the endpoint; nothing opens automatically)"

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        cursorShape: (tile.occupied && tile.peekable) ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: if (tile.occupied && tile.peekable) tile.peekRequested()
    }
}
